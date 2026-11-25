"""An accepted early-payment discount changes what is PAID, not just what is
reported.

Accepting a `DiscountOffer` is the buyer agreeing to pay the discounted amount
before the deadline. Until migration 0104 nothing in the payment path read the
offer: a run paid the invoice net of credit memos, and the only way to pay the
discounted figure was to record a credit memo for the savings by hand.

`payment_runs.payable_amounts` is now the one answer every money path books
and re-checks against — the run builder, the standalone `POST /api/payments`,
dispatch (re-asked on the day the money moves) and `/retry-failed`. Pinned
here, against real Postgres rows and the real HTTP surface:

* the run, the standalone payment and the queue all carry the discounted
  figure, exactly (Decimal), with the offer linked on the payment and a
  `discount_offer.applied` audit row;
* the settlement captures exactly the booked offer, stamped with the payment,
  and a void reverses exactly that capture;
* a deadline that passes while the run waits fails the payment retry-safe as
  `discount_changed` before any adapter call — never a silent re-price — and
  `/retry-failed` will not re-send it;
* a discount taken through a credit memo that names the offer is not
  deducted a second time, while a return credit that merely equals the
  savings is an ordinary credit and suppresses nothing (decisions §520);
* under a race, two creates linking one offer, or two offers on one invoice,
  write exactly one linked memo (the invoice and offer row locks), and the
  partial unique index's IntegrityError is the same coded 409, never a 500;
* an offer that cannot be honoured (expired, other currency, base amount no
  longer the invoice's, vendor-scoped) pays the invoice in full.

Runs against the opt-in `realdb` fixture.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from app.models.credit_memo import CreditMemo
from app.models.discount import DiscountOffer
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.utils.dates import utc_today

TENANT = "a"


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _seed_invoice(mk, org_id, *, amount="1037.00", currency="USD") -> tuple[str, str]:
    """An approved invoice from an active vendor. Not a round figure, so the
    round-amount fraud rule never puts a payment-blocking flag on it."""
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        vendor = Vendor(organization_id=org_id, name="Discount Pay Vendor", entity_id=entity_id)
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=f"DISC-PAY-{uuid.uuid4().hex[:8]}",
            vendor_name=vendor.name,
            vendor_id=vendor.id,
            amount=Decimal(amount),
            currency=currency,
            due_date=utc_today() + timedelta(days=30),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        return str(vendor.id), str(inv.id)


async def _accepted_offer(c, invoice_id: str, *, percent: str = "2.00", days: int = 10) -> str:
    resp = await c.post(
        "/api/discounts/offers",
        json={
            "scope": "invoice",
            "invoice_id": invoice_id,
            "tiers": [{"days": days, "percent": percent}],
        },
    )
    assert resp.status_code == 201, resp.text
    offer_id = resp.json()["id"]
    accept = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
    assert accept.status_code == 200, accept.text
    return offer_id


async def _backdate(mk, offer_id: str, days: int) -> None:
    """Move an offer's reference date into the past, so its tier deadline is."""
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        offer.valid_from = utc_today() - timedelta(days=days)
        await s.commit()


async def _create_run(realdb, invoice_id: str) -> dict:
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/payments/runs",
            json={"items": [{"invoice_id": invoice_id, "method": "ach"}]},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _execute(realdb, run_id: str) -> dict:
    # Run segregation: a different user from the creator executes.
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _payment(mk, invoice_id: str) -> Payment:
    async with mk() as s:
        return (
            (
                await s.execute(
                    select(Payment)
                    .where(Payment.invoice_id == uuid.UUID(invoice_id))
                    .order_by(Payment.created_at)
                )
            )
            .scalars()
            .all()
        )[-1]


# --------------------------------------------------------------------------- #
# The run pays the discounted figure, end to end
# --------------------------------------------------------------------------- #


async def test_run_books_executes_and_captures_the_discounted_amount(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)

    # 2 % of 1,037.00 = 20.74 exactly (half-up to the cent).
    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1016.26"
    assert run["discount_total"] == "20.74"

    payment = await _payment(mk, invoice_id)
    assert payment.amount == Decimal("1016.26")
    assert payment.discount_amount == Decimal("20.74")
    assert str(payment.discount_offer_id) == offer_id

    async with mk() as s:
        applied = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.applied",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
    assert applied.details == {
        "invoice_id": invoice_id,
        "payment_id": str(payment.id),
        "payment_run_id": run["id"],
        "invoice_amount": "1037.00",
        "net_before_discount": "1037.00",
        "discount_amount": "20.74",
        "payment_amount": "1016.26",
        "pay_by": applied.details["pay_by"],
    }
    assert date.fromisoformat(applied.details["pay_by"]) >= utc_today()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        detail = (await c.get(f"/api/payments/runs/{run['id']}")).json()
    assert detail["discount_total"] == "20.74"
    (row,) = detail["payments"]
    assert row["amount"] == "1016.26"
    assert row["discount_amount"] == "20.74"
    assert row["invoice_amount"] == "1037.00"

    result = await _execute(realdb, run["id"])
    assert result["payments_completed"] == 1

    payment = await _payment(mk, invoice_id)
    assert payment.status == "completed"
    assert payment.amount == Decimal("1016.26")
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "captured"
        assert offer.captured_amount == Decimal("20.74")
        assert offer.captured_by_payment_id == payment.id


async def test_the_queue_shows_what_a_run_would_pay(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)
        items = (await c.get("/api/payments/queue", params={"page_size": 100})).json()["items"]
    (row,) = [i for i in items if i["id"] == invoice_id]
    assert row["amount"] == "1037.00"  # the invoice, unchanged
    assert row["accepted_discount_amount"] == "20.74"
    assert row["payable_amount"] == "1016.26"
    assert date.fromisoformat(row["accepted_discount_pay_by"]) >= utc_today()


async def test_an_undiscounted_queue_row_pays_its_net(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        items = (await c.get("/api/payments/queue", params={"page_size": 100})).json()["items"]
    (row,) = [i for i in items if i["id"] == invoice_id]
    assert row["accepted_discount_amount"] is None
    assert row["accepted_discount_pay_by"] is None
    assert row["payable_amount"] == "1037.00"


# --------------------------------------------------------------------------- #
# The standalone payment takes it the same way
# --------------------------------------------------------------------------- #


async def test_standalone_payment_pays_the_discounted_amount_and_refuses_the_gross(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        gross = await c.post(
            "/api/payments",
            json={"invoice_id": invoice_id, "amount": "1037.00", "method": "ach"},
        )
        assert gross.status_code == 422, gross.text
        net = await c.post(
            "/api/payments",
            json={"invoice_id": invoice_id, "amount": "1016.26", "method": "ach"},
        )
    assert net.status_code == 201, net.text
    body = net.json()
    assert body["discount_offer_id"] == offer_id
    assert Decimal(str(body["discount_amount"])) == Decimal("20.74")

    payment = await _payment(mk, invoice_id)
    assert payment.amount == Decimal("1016.26")
    assert payment.discount_amount == Decimal("20.74")
    async with mk() as s:
        applied = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.applied",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
    assert applied.details["payment_run_id"] is None
    assert applied.details["payment_amount"] == "1016.26"


# --------------------------------------------------------------------------- #
# The deadline is re-checked on the day the money moves
# --------------------------------------------------------------------------- #


async def test_a_deadline_missed_while_the_run_waited_refuses_dispatch_retry_safe(realdb):
    """Booked at the discount, then the tier's deadline passes before execute
    (a draft waiting for CFO sign-off). Moving the discounted figure would
    short-pay a supplier who considers the offer dead; moving the full figure
    would be money nobody approved. Neither: `discount_changed`, before the
    adapter is called, and `/retry-failed` keeps skipping it."""
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1016.26"

    await _backdate(mk, offer_id, days=30)

    from app.services.payment_adapters.mock_adapter import MockPaymentAdapter

    with patch.object(MockPaymentAdapter, "create_payment") as create:
        result = await _execute(realdb, run["id"])
    create.assert_not_called()
    assert result["payments_completed"] == 0

    payment = await _payment(mk, invoice_id)
    assert payment.status == "failed"
    assert payment.failure_reason == "discount_changed"
    async with mk() as s:
        assert (await s.get(DiscountOffer, uuid.UUID(offer_id))).status == "accepted"
        assert (await s.get(Invoice, uuid.UUID(invoice_id))).status == InvoiceStatus.approved

    async with realdb.client(key=TENANT, role="admin") as c:
        retry = await c.post(f"/api/payments/runs/{run['id']}/retry-failed")
    assert retry.status_code == 200, retry.text
    assert retry.json()["payments_retried"] == 0
    assert retry.json()["skip_reasons"] == ["discount_changed"]

    # A fresh run books what is owed now: the full amount, no offer. (A
    # `failed` payment is terminal, so it claims nothing.)
    fresh = await _create_run(realdb, invoice_id)
    assert fresh["total_amount"] == "1037.00"
    assert fresh["discount_total"] == "0"


async def test_an_offer_accepted_after_booking_is_not_paid_past(realdb):
    """The run was booked (and approved) at the full figure; a discount accepted
    afterwards changes what is owed, so dispatch refuses rather than move a
    figure the supplier has agreed to reduce."""
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1037.00"
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)

    result = await _execute(realdb, run["id"])
    assert result["payments_completed"] == 0
    payment = await _payment(mk, invoice_id)
    assert payment.failure_reason == "discount_changed"


async def test_an_offer_already_expired_at_booking_pays_in_full(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    await _backdate(mk, offer_id, days=30)

    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1037.00"
    payment = await _payment(mk, invoice_id)
    assert payment.discount_offer_id is None
    assert payment.discount_amount is None

    await _execute(realdb, run["id"])
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "accepted"  # nothing was saved, nothing captured


# --------------------------------------------------------------------------- #
# Void reverses exactly its own capture
# --------------------------------------------------------------------------- #


async def test_voiding_a_discounted_payment_reverses_its_own_capture(realdb):
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    await _execute(realdb, run["id"])
    payment = await _payment(mk, invoice_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/payments/{payment.id}/void", json={"reason": "Re-paying."})
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "accepted"
        assert offer.captured_amount is None
        assert offer.captured_by_payment_id is None
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.capture_reversed",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
        assert audit.details["reversed_amount"] == "20.74"


# --------------------------------------------------------------------------- #
# Never deducted twice, never deducted when it can't be honoured
# --------------------------------------------------------------------------- #


async def _memo(c, vendor_id: str, invoice_id: str, amount: str, **extra):
    return await c.post(
        "/api/credit-memos",
        json={
            "memo_number": f"CM-{uuid.uuid4().hex[:6]}",
            "vendor_id": vendor_id,
            "amount": amount,
            "invoice_id": invoice_id,
            **extra,
        },
    )


async def test_a_discount_taken_by_a_linked_credit_memo_is_not_deducted_again(realdb):
    """A credit memo that NAMES the offer (`discount_offer_id`, decisions §520)
    is the discount taken: it is already in the net, so the offer must not
    come off a second time (short-paying the supplier by the discount), and
    the settlement captures the offer through the memo, for the memo's
    amount, stamped with the payment."""
    mk = realdb.sessionmaker(TENANT)
    vendor_id, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
        memo = await _memo(c, vendor_id, invoice_id, "20.74", discount_offer_id=offer_id)
        assert memo.status_code == 201, memo.text
        assert memo.json()["discount_offer_id"] == offer_id
        assert memo.json()["status"] == "applied"

    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1016.26"
    payment = await _payment(mk, invoice_id)
    assert payment.discount_offer_id is None

    await _execute(realdb, run["id"])
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "captured"
        assert offer.captured_amount == Decimal("20.74")
        assert offer.captured_by_payment_id == payment.id
        created = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "credit_memo.created",
                    AuditLog.entity_id == uuid.UUID(memo.json()["id"]),
                )
            )
        ).scalar_one()
        assert created.details["discount_offer_id"] == offer_id


async def test_a_return_credit_equal_to_the_savings_does_not_suppress_the_discount(realdb):
    """The defect §520 closes: §250 read ANY applied memo for exactly the
    savings as the discount already taken. A return credit that happens to
    equal the savings is an ordinary credit — both come off, the payment books
    the offer itself, and the settlement captures it through that booking, not
    through the memo."""
    mk = realdb.sessionmaker(TENANT)
    vendor_id, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
        memo = await _memo(c, vendor_id, invoice_id, "20.74", reason="Returned goods")
        assert memo.status_code == 201, memo.text
        assert memo.json()["discount_offer_id"] is None

    run = await _create_run(realdb, invoice_id)
    # 1037.00 - 20.74 (return) - 20.74 (2% discount on the 1037.00 base).
    assert run["total_amount"] == "995.52"
    payment = await _payment(mk, invoice_id)
    assert payment.discount_offer_id == uuid.UUID(offer_id)
    assert payment.discount_amount == Decimal("20.74")

    await _execute(realdb, run["id"])
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "captured"
        assert offer.captured_by_payment_id == payment.id


async def test_a_return_credit_equal_to_the_savings_never_captures_a_lapsed_offer(realdb):
    """The capture half of the same defect: once the deadline has passed the
    run pays net of the return credit — exactly the old discounted payoff —
    and the amount-match fallback used to record the offer as captured. With
    no memo naming the offer, nothing is captured."""
    mk = realdb.sessionmaker(TENANT)
    vendor_id, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
        memo = await _memo(c, vendor_id, invoice_id, "20.74", reason="Returned goods")
        assert memo.status_code == 201, memo.text
    await _backdate(mk, offer_id, 30)

    run = await _create_run(realdb, invoice_id)
    assert run["total_amount"] == "1016.26"
    await _execute(realdb, run["id"])
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.status == "accepted"
        assert offer.captured_amount is None


@pytest.mark.parametrize(
    "variant",
    ["no_invoice", "wrong_amount", "other_invoice", "second_memo", "booked", "paid_in_full"],
)
async def test_a_memo_cannot_take_a_discount_it_is_not(realdb, variant):
    """The link is the proof, so it is refused unless the memo is exactly the
    discount: on the offer's own invoice, for the savings to the cent, the
    only live discount memo on that invoice, and with no live payment on the
    invoice (one booked with the offer would deduct it twice; one sent at the
    full amount would capture savings nobody took). Nothing is written on a
    refusal."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, invoice_id = await _seed_invoice(mk, org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
        body = {
            "memo_number": f"CM-{uuid.uuid4().hex[:6]}",
            "vendor_id": vendor_id,
            "amount": "20.74",
            "invoice_id": invoice_id,
            "discount_offer_id": offer_id,
        }
        expected = 409
        if variant == "no_invoice":
            body.pop("invoice_id")
        elif variant == "wrong_amount":
            body["amount"] = "20.73"
        elif variant == "other_invoice":
            _, other_invoice = await _seed_invoice(mk, org_id)
            other_offer = await _accepted_offer(c, other_invoice)
            body["discount_offer_id"] = other_offer
            expected = 404
        elif variant == "second_memo":
            second_offer = await _accepted_offer(c, invoice_id)
            first = await _memo(c, vendor_id, invoice_id, "20.74", discount_offer_id=second_offer)
            assert first.status_code == 201, first.text
        elif variant == "booked":
            await _create_run(realdb, invoice_id)
            payment = await _payment(mk, invoice_id)
            assert payment.discount_offer_id == uuid.UUID(offer_id)
        elif variant == "paid_in_full":
            # Deadline passed: the run books the full amount, no discount.
            # Linking now would let that payment's settlement capture savings
            # nobody took (money-path review, §520).
            await _backdate(mk, offer_id, 30)
            run = await _create_run(realdb, invoice_id)
            assert run["total_amount"] == "1037.00"
        resp = await c.post("/api/credit-memos", json=body)
    assert resp.status_code == expected, resp.text
    if expected == 409:
        assert resp.json()["detail"]["code"] == "credit_memo_discount_offer_invalid"
    async with mk() as s:
        linked = (
            (
                await s.execute(
                    select(CreditMemo).where(CreditMemo.discount_offer_id == uuid.UUID(offer_id))
                )
            )
            .scalars()
            .all()
        )
    assert linked == []


# --------------------------------------------------------------------------- #
# The link under a race: one discount per offer, one per invoice
# --------------------------------------------------------------------------- #


async def _wait_for_lock_waiter(mk, *, timeout: float = 15.0) -> bool:
    """True once Postgres reports another backend in this database waiting on
    a lock (False after `timeout`). Asked of `pg_stat_activity` from an
    independent session, the real signal rather than a sleep, as
    `test_payment_concurrency` and `test_credit_memos` do."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.05)
    return False


async def _live_linked_memos(mk, invoice_id: str) -> list[CreditMemo]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(CreditMemo).where(
                        CreditMemo.invoice_id == uuid.UUID(invoice_id),
                        CreditMemo.discount_offer_id.is_not(None),
                        CreditMemo.status != "void",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _race_linked_creates(realdb, vendor_id, invoice_id, first_offer, second_offer):
    """Two `POST /api/credit-memos` linking a discount on one invoice, each on
    its own connection (the harness engine is NullPool). The first is parked
    inside its transaction, after its memo is flushed and its invoice and offer
    rows are locked, while the second starts; the second must then be WAITING
    ON A LOCK (a plain read would not wait) before the first is let commit.
    Deterministic by construction: the order is fixed, never a timing guess."""
    from app.api import credit_memos as credit_memos_api

    mk = realdb.sessionmaker(TENANT)
    real_dispatch = credit_memos_api.dispatch_audit
    parked = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _gated_dispatch(db, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            parked.set()
            await release.wait()
        return await real_dispatch(db, **kwargs)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        with patch.object(credit_memos_api, "dispatch_audit", _gated_dispatch):
            first = asyncio.create_task(
                _memo(c, vendor_id, invoice_id, "20.74", discount_offer_id=first_offer)
            )
            second = None
            try:
                await asyncio.wait_for(parked.wait(), timeout=15)
                second = asyncio.create_task(
                    _memo(c, vendor_id, invoice_id, "20.74", discount_offer_id=second_offer)
                )
                queued = await _wait_for_lock_waiter(mk)
            finally:
                # Never leave the first racer parked holding its row locks: the
                # harness's per-test TRUNCATE would queue behind it forever.
                release.set()
                results = await asyncio.gather(*[t for t in (first, second) if t is not None])
    assert queued, "the second create did not wait on the first one's row lock"
    return results


async def test_two_concurrent_memos_cannot_take_one_offer_twice(realdb):
    """The §520 race on one offer: two creates naming the same offer, the
    second arriving while the first holds its locks. Exactly one memo is
    written; the other is the coded 409. Two would short-pay the supplier by
    the discount twice."""
    mk = realdb.sessionmaker(TENANT)
    vendor_id, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)

    won, lost = await _race_linked_creates(realdb, vendor_id, invoice_id, offer_id, offer_id)

    assert won.status_code == 201, won.text
    assert lost.status_code == 409, lost.text
    assert lost.json()["detail"]["code"] == "credit_memo_discount_offer_invalid"
    linked = await _live_linked_memos(mk, invoice_id)
    assert [str(m.id) for m in linked] == [won.json()["id"]]
    assert linked[0].discount_offer_id == uuid.UUID(offer_id)
    assert linked[0].amount == Decimal("20.74")


async def test_two_offers_on_one_invoice_cannot_both_be_taken_concurrently(realdb):
    """One invoice takes one discount: two creates naming two different
    accepted offers on the same invoice, racing. The partial unique index is
    per offer and cannot see this, so the invoice row lock is what serializes
    them; the second re-reads and refuses."""
    mk = realdb.sessionmaker(TENANT)
    vendor_id, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first_offer = await _accepted_offer(c, invoice_id)
        second_offer = await _accepted_offer(c, invoice_id)

    won, lost = await _race_linked_creates(realdb, vendor_id, invoice_id, first_offer, second_offer)

    assert won.status_code == 201, won.text
    assert lost.status_code == 409, lost.text
    assert lost.json()["detail"]["code"] == "credit_memo_discount_offer_invalid"
    linked = await _live_linked_memos(mk, invoice_id)
    assert [str(m.id) for m in linked] == [won.json()["id"]]
    assert linked[0].discount_offer_id == uuid.UUID(first_offer)


async def test_the_unique_index_backstop_answers_the_coded_409(realdb, monkeypatch):
    """If the locks and the pre-check ever let a second linked memo through,
    `uq_credit_memos_discount_offer_live` refuses the insert, and the route
    answers it as the same coded 409, not a 500, with nothing of the second
    memo written: no row, no `credit_memo.created` audit row."""
    from app.api import credit_memos as credit_memos_api

    mk = realdb.sessionmaker(TENANT)
    vendor_id, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
        first = await _memo(c, vendor_id, invoice_id, "20.74", discount_offer_id=offer_id)
        assert first.status_code == 201, first.text

        async def _no_precheck(db, **kwargs):
            return None

        monkeypatch.setattr(credit_memos_api, "_assert_discount_offer_linkable", _no_precheck)
        number = f"CM-{uuid.uuid4().hex[:6]}"
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": number,
                "vendor_id": vendor_id,
                "amount": "20.74",
                "invoice_id": invoice_id,
                "discount_offer_id": offer_id,
            },
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "credit_memo_discount_offer_invalid"
    async with mk() as s:
        memos = (
            (
                await s.execute(
                    select(CreditMemo).where(CreditMemo.invoice_id == uuid.UUID(invoice_id))
                )
            )
            .scalars()
            .all()
        )
        assert [str(m.id) for m in memos] == [first.json()["id"]]
        created = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "credit_memo.created")))
            .scalars()
            .all()
        )
    assert [a.entity_id for a in created] == [uuid.UUID(first.json()["id"])]
    assert number not in {a.details["memo_number"] for a in created}


@pytest.mark.parametrize("variant", ["currency", "base", "vendor_scope", "no_tier"])
async def test_an_offer_that_cannot_be_honoured_pays_in_full(realdb, variant):
    from app.services.payment_runs import payable_amount

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, invoice_id = await _seed_invoice(mk, org_id)
    async with mk() as s:
        inv = await s.get(Invoice, uuid.UUID(invoice_id))
        s.add(
            DiscountOffer(
                organization_id=org_id,
                entity_id=inv.entity_id,
                scope="vendor" if variant == "vendor_scope" else "invoice",
                invoice_id=inv.id,
                vendor_id=uuid.UUID(vendor_id),
                base_amount=Decimal("900.00") if variant == "base" else Decimal("1037.00"),
                currency="EUR" if variant == "currency" else "USD",
                tiers=[{"days": 10, "percent": "2.00"}],
                status="accepted",
                accepted_tier=None if variant == "no_tier" else {"days": 10, "percent": "2.00"},
                valid_from=utc_today(),
            )
        )
        await s.commit()
        payable = await payable_amount(s, inv, pay_date=utc_today())
    assert payable.discount is None
    assert payable.amount == Decimal("1037.00")


async def test_a_discount_that_would_consume_the_whole_net_is_not_taken(realdb):
    """Credits already cover all but 10.00; a 20.74 deduction would leave a
    negative payment. The supplier is owed the 10.00."""
    from app.services.payment_runs import payable_amount

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, invoice_id = await _seed_invoice(mk, org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)
    async with mk() as s:
        s.add(
            CreditMemo(
                memo_number=f"CM-{uuid.uuid4().hex[:6]}",
                vendor_id=uuid.UUID(vendor_id),
                invoice_id=uuid.UUID(invoice_id),
                amount=Decimal("1027.00"),
                currency="USD",
                status="applied",
                organization_id=org_id,
            )
        )
        await s.commit()
        inv = await s.get(Invoice, uuid.UUID(invoice_id))
        payable = await payable_amount(s, inv, pay_date=utc_today())
    assert payable.discount is None
    assert payable.amount == Decimal("10.00")


async def test_the_deadline_day_itself_still_earns_the_discount(realdb):
    """`pay_date <= deadline`: paying ON the last day is in time."""
    from app.services.discount_offers import accepted_discount_deadline
    from app.services.payment_runs import payable_amount

    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        deadline = accepted_discount_deadline(offer)
        inv = await s.get(Invoice, uuid.UUID(invoice_id))
        on_time = await payable_amount(s, inv, pay_date=deadline)
        late = await payable_amount(s, inv, pay_date=deadline + timedelta(days=1))
    assert on_time.discount_amount == Decimal("20.74")
    assert late.discount is None


# --------------------------------------------------------------------------- #
# Downstream figures read the booked amount
# --------------------------------------------------------------------------- #


async def test_1099_totals_report_the_discounted_amount_paid(realdb):
    """The 1099 box amount is what was actually paid. It sums `Payment.amount`,
    which is now the discounted figure — so the discount is never reported as
    income the vendor did not receive."""
    from app.services.tax_1099 import build_1099_report

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id, invoice_id = await _seed_invoice(mk, org_id)
    async with mk() as s:
        vendor = await s.get(Vendor, uuid.UUID(vendor_id))
        vendor.is_1099_eligible = True
        await s.commit()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    await _execute(realdb, run["id"])

    async with mk() as s:
        report = await build_1099_report(
            s, org_id, utc_today().year, reporting_currency="USD", org_settings={}
        )
    (row,) = [r for r in report.rows if str(r.vendor_id) == vendor_id]
    assert row.ytd_paid == Decimal("1016.26")


async def test_deleting_either_side_of_the_link_sets_it_null(realdb):
    """Migration 0104's `ON DELETE SET NULL`, both directions. The payments that
    are ever deleted are the still-`pending` rows of a cancelled draft run (and
    test cleanup); a RESTRICT would turn that into an FK error. Deleting an
    offer a payment names must not take the payment's money record with it."""
    mk = realdb.sessionmaker(TENANT)
    _, invoice_id = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id = await _accepted_offer(c, invoice_id)
    run = await _create_run(realdb, invoice_id)
    await _execute(realdb, run["id"])
    payment = await _payment(mk, invoice_id)

    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer.captured_by_payment_id == payment.id
        await s.execute(Payment.__table__.delete().where(Payment.id == payment.id))
        await s.commit()
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        assert offer is not None
        assert offer.captured_by_payment_id is None

    _, invoice_id2 = await _seed_invoice(mk, realdb.info(TENANT).org_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offer_id2 = await _accepted_offer(c, invoice_id2)
    await _create_run(realdb, invoice_id2)
    async with mk() as s:
        await s.execute(
            DiscountOffer.__table__.delete().where(DiscountOffer.id == uuid.UUID(offer_id2))
        )
        await s.commit()
    payment2 = await _payment(mk, invoice_id2)
    assert payment2.discount_offer_id is None
    assert payment2.discount_amount == Decimal("20.74")
