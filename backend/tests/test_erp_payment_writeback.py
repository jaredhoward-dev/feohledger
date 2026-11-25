"""Phase 4: a settled FeohLedger payment written back to QuickBooks (§295).

Driven end to end through `payment_erp_sync._sync_payments` over the `realdb`
tenant pair, with the ERP adapter faked at `get_erp_adapter`: the leg moves the
invoice to `paid`, THEN writes the payment back against the bill the invoice was
posted as, from the pay-from account the org mapped (our GL code, resolved to
its QuickBooks id under the current realm). A refusal is recorded coded and
flagged, never undoes the transition; a re-run never writes twice; a bill in
another QuickBooks company is never written to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.exception import Exception as APException
from app.models.gl_account import GLAccount
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.virtual_card import VirtualCard
from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowInstance
from app.services import erp_payment_writeback
from app.services.erp_adapters.base import ErpPostResult, erp_preflight_refusal, erp_refusal
from app.services.payment_erp_sync import _sync_payments
from app.services.payment_settlement_record import CARD_SETTLED_STATUS

COMPANY = "9130"
REALM = f"quickbooks_online:{COMPANY}"


@pytest.fixture(autouse=True)
def _control_db(realdb, monkeypatch):
    # `_sync_payments` builds its own engines off `settings.database_url`
    # (see test_payment_erp_sync.py's fixture of the same name).
    monkeypatch.setattr(settings, "database_url", realdb.control_db_url())


class FakeQbo:
    erp_type = "quickbooks_online"
    supports_status_sync = True

    def __init__(self, result: ErpPostResult | None = None):
        self.post_payment = AsyncMock(
            return_value=result
            or ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal(0))
        )
        self.void_payment = AsyncMock(
            return_value=ErpPostResult(success=True, erp_document_id="BP-9")
        )


async def _configure(realdb, **erp_extra) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "quickbooks_online",
                "integration_method": "direct",
                "bill_payment_bank_account": "1010",
                "bill_payment_card_account": "2050",
                "oauth": {
                    "provider": "quickbooks_online",
                    "external_tenant_id": COMPANY,
                    "org_id": str(org.id),
                    "connection_id": "conn-1",
                },
                **erp_extra,
            },
        }
        await s.commit()


async def _seed(realdb, *, method="ach", realm=REALM, bill="501"):
    info = realdb.info("a")
    run_id, inv_id, pay_id, corr = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        s.add(PaymentRun(id=run_id, status="completed", organization_id=info.org_id))
        s.add(
            Invoice(
                id=inv_id,
                correlation_id=corr,
                invoice_number=f"WB-{uuid.uuid4().hex[:6]}",
                vendor_name="Vendor Co",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.payment_scheduled,
                organization_id=info.org_id,
            )
        )
        for code, erp_id in (("1010", "35"), ("2050", "41")):
            s.add(
                GLAccount(
                    organization_id=info.org_id,
                    code=code,
                    name=code,
                    erp_account_id=erp_id,
                    erp_link_realm=REALM,
                )
            )
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=corr,
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": bill, "erp_realm": realm},
            )
        )
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("100.00"),
                method=method,
                status="completed",
                completed_at=datetime(2026, 10, 2, 15, 0, tzinfo=UTC),
            )
        )
        await s.commit()
    return run_id, inv_id, pay_id


async def _sync(realdb, run_id, adapter):
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        return await _sync_payments(run_id, realdb.info("a").org_id)


async def _rows(realdb, model, **where):
    async with realdb.sessionmaker("a")() as s:
        stmt = select(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return (await s.execute(stmt)).scalars().all()


async def test_a_settled_payment_is_written_back_against_its_bill(realdb):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()

    result = await _sync(realdb, run_id, adapter)

    assert result.transitioned == 1
    [invoice] = await _rows(realdb, Invoice, id=inv_id)
    assert invoice.status == InvoiceStatus.paid
    (payload,) = adapter.post_payment.await_args.args
    assert payload.payment_id == str(pay_id)
    assert payload.bill_id == "501"
    assert payload.amount == Decimal("100.00") and payload.currency == "USD"
    assert payload.paid_on.isoformat() == "2026-10-02"
    # The org's mapped bank account, as QuickBooks knows it.
    assert payload.pay_from_account_erp_id == "35" and payload.card is False
    actions = {r.action for r in await _rows(realdb, AuditLog, entity_id=pay_id)}
    assert "payment.erp_posted" in actions
    assert await _rows(realdb, APException, invoice_id=inv_id) == []


async def test_a_card_payment_uses_the_card_account(realdb):
    await _configure(realdb)
    run_id, _, pay_id = await _seed(realdb, method="virtual_card")
    adapter = FakeQbo()
    # Uncharged, the card leg has moved nothing: no write-back (decisions §537).
    await _sync(realdb, run_id, adapter)
    adapter.post_payment.assert_not_awaited()
    # The charge settles and the card webhook writes it onto the leg (only a
    # settled charge counts, decisions §543); the next pass writes back.
    async with realdb.sessionmaker("a")() as s:
        payment = await s.get(Payment, pay_id)
        invoice = await s.get(Invoice, payment.invoice_id)
        s.add(
            VirtualCard(
                invoice_id=invoice.id,
                payment_id=pay_id,
                organization_id=realdb.info("a").org_id,
                entity_id=invoice.entity_id,
                correlation_id=payment.correlation_id,
                card_provider="mock",
                provider_card_id=f"card-{uuid.uuid4().hex[:10]}",
                last_four="9999",
                amount_limit=payment.amount,
                amount_charged=Decimal("100.00"),
                currency="USD",
                status=CARD_SETTLED_STATUS,
            )
        )
        payment.settled_amount = Decimal("100.00")
        payment.settled_currency = "USD"
        await s.commit()
    await _sync(realdb, run_id, adapter)
    (payload,) = adapter.post_payment.await_args.args
    assert payload.card is True and payload.pay_from_account_erp_id == "41"


async def test_an_unlinked_pay_from_account_reaches_the_adapter_as_unmapped(realdb):
    await _configure(realdb, bill_payment_bank_account="9999")
    run_id, _, _ = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    (payload,) = adapter.post_payment.await_args.args
    assert payload.pay_from_account_erp_id is None  # the adapter refuses it


async def test_a_refused_write_back_is_coded_flagged_and_keeps_the_invoice_paid(realdb):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    refused = erp_refusal("QuickBooks Online", "payment_account_not_mapped")
    await _sync(realdb, run_id, FakeQbo(refused))

    [invoice] = await _rows(realdb, Invoice, id=inv_id)
    assert invoice.status == InvoiceStatus.paid  # the money moved
    [row] = [r for r in await _rows(realdb, AuditLog, entity_id=pay_id) if "erp_post" in r.action]
    assert row.action == "payment.erp_post_failed"
    assert row.details["refusal"]["code"] == "payment_account_not_mapped"
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.exception_type == "erp_reconciliation"
    # The sentence is for an AP user, in a catalogue code; the refusal, payment,
    # run and bill are data on erp_report (decisions §319, §331).
    assert flag.description_code == "exception.erp_payment_writeback_failed"
    assert flag.description_params == {"erp": "QuickBooks Online"}
    for leaked in ("payment_account_not_mapped", str(pay_id), str(run_id), "/api/", "501"):
        assert leaked not in flag.description
    report = flag.erp_report
    assert report["kind"] == "payment_writeback_failed"
    assert (report["payment_id"], report["payment_run_id"]) == (str(pay_id), str(run_id))
    assert report["erp_document_id"] == "501"
    assert report["failure"] == "payment_account_not_mapped"
    assert report["refusal"]["code"] == "payment_account_not_mapped"


async def test_a_failed_write_back_with_no_refusal_code_is_still_explained(realdb):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    await _sync(realdb, run_id, FakeQbo(ErpPostResult(success=False, message="x", retryable=True)))
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.description_code == "exception.erp_payment_writeback_failed"
    assert flag.erp_report["failure"] == "erp_post_failed"
    assert flag.erp_report["refusal"] is None
    assert flag.erp_report["retryable"] is True


async def test_a_bill_left_partly_open_in_the_erp_is_flagged(realdb):
    await _configure(realdb)
    run_id, inv_id, _ = await _seed(realdb)
    left = ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal("2.00"))
    await _sync(realdb, run_id, FakeQbo(left))
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.exception_type == "erp_reconciliation"
    assert flag.description_code == "exception.erp_payment_writeback_balance_open"
    assert flag.description_params == {
        "erp": "QuickBooks Online",
        "amount": "2.00",
        "currency": "USD",
    }
    assert "2.00 USD" in flag.description and "bill=" not in flag.description
    assert flag.erp_report["kind"] == "payment_writeback_balance_open"
    assert flag.erp_report["bill_balance_after"] == "2.00"
    assert flag.erp_report["erp_payment_id"] == "BP-9"


async def test_a_discount_left_open_in_the_erp_is_named_on_the_flag(realdb):
    """No ERP payment carries a discount (QuickBooks, Xero; §295, §386): the
    amount that reached the supplier is posted, and the flag names the
    discount the bookkeeper still has to record there."""
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    async with realdb.sessionmaker("a")() as s:
        payment = await s.get(Payment, pay_id)
        payment.amount = Decimal("98.00")
        payment.discount_amount = Decimal("2.00")
        await s.commit()
    adapter = FakeQbo(
        ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal("2.00"))
    )
    await _sync(realdb, run_id, adapter)
    [payload] = [c.args[0] for c in adapter.post_payment.await_args_list]
    assert payload.amount == Decimal("98.00")
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.erp_report["kind"] == "payment_writeback_balance_open"
    assert flag.erp_report["discount_amount"] == "2.00"


async def test_a_bill_in_another_quickbooks_company_is_never_written_to(realdb):
    await _configure(realdb)
    run_id, inv_id, _ = await _seed(realdb, realm="quickbooks_online:sandbox-1")
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    adapter.post_payment.assert_not_awaited()
    [invoice] = await _rows(realdb, Invoice, id=inv_id)
    assert invoice.status == InvoiceStatus.paid


async def test_a_retry_of_the_sync_reaches_the_adapter_with_the_same_key(realdb):
    """`/sync-erp` re-runs the pass; the invoice is already paid, the leg
    writes back again, and the adapter's own idempotency (the payment id) is
    what keeps QuickBooks at one bill payment."""
    await _configure(realdb)
    run_id, _, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    await _sync(realdb, run_id, adapter)
    keys = [call.args[0].payment_id for call in adapter.post_payment.await_args_list]
    assert keys == [str(pay_id), str(pay_id)]


async def test_voiding_the_payment_voids_what_was_written(realdb, monkeypatch):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)

    # The void write-back reads the org through the shared control-plane
    # factory, which the harness points at this slot.
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.WROTE
    (payload,) = adapter.void_payment.await_args.args
    assert payload.payment_id == str(pay_id) and payload.bill_id == "501"
    actions = {r.action for r in await _rows(realdb, AuditLog, entity_id=pay_id)}
    assert "payment.erp_voided" in actions

    adapter.void_payment.return_value = ErpPostResult(success=False, message="x", retryable=True)
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.FAILED
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.exception_type == "erp_reconciliation"
    assert flag.description_code == "exception.erp_payment_void_writeback_failed"
    assert str(pay_id) not in flag.description and "erp_void_failed" not in flag.description
    assert flag.erp_report["kind"] == "payment_void_writeback_failed"
    assert flag.erp_report["payment_id"] == str(pay_id)
    assert flag.erp_report["payment_run_id"] == str(run_id)
    assert flag.erp_report["failure"] == "erp_void_failed"


async def test_a_payment_voided_before_the_write_back_is_not_written(realdb):
    """Re-read before the ERP call: a void that committed after the sync's
    transition must not leave a BillPayment for a voided payment."""
    await _configure(realdb)
    _, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    async with realdb.sessionmaker("a")() as s:
        payment = await s.get(Payment, pay_id)
        invoice = await s.get(Invoice, inv_id)
        await s.execute(
            Payment.__table__.update().where(Payment.id == pay_id).values(status="voided")
        )
        await s.commit()
        state = {"erp_reference": "501", "erp_realm": REALM}
        async with realdb.control_sessionmaker()() as c:
            org = await c.get(Organization, realdb.info("a").org_id)
        outcome = await erp_payment_writeback.write_back_payment(
            s,
            adapter=adapter,
            erp_config=org.settings["erp"],
            payment=payment,
            invoice=invoice,
            state_data=state,
        )
    assert outcome == erp_payment_writeback.SKIPPED
    adapter.post_payment.assert_not_awaited()


async def test_the_void_records_its_erp_void_and_the_sweep_finishes_it(realdb):
    """The intent to void in the ERP is written in the void's own transaction,
    so a process that dies before the background task still leaves it to the
    status-sync sweep (money-path review, §295)."""
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)

    # The void itself, with the background task suppressed (the process "died").
    with patch("app.services.erp_payment_writeback.dispatch_void_writeback") as dispatched:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(f"/api/payments/{pay_id}/void", json={"reason": "wrong vendor"})
    assert resp.status_code == 200, resp.text
    dispatched.assert_called_once()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.PENDING_VOIDS_KEY] == [str(pay_id)]

    async with realdb.control_sessionmaker()() as c:
        org = await c.get(Organization, realdb.info("a").org_id)
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        left = await erp_payment_writeback.resume_pending_voids(org)
    assert left == 0
    adapter.void_payment.assert_awaited_once()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_VOIDS_KEY not in instance.state_data


async def test_voiding_a_payment_on_a_mock_pushed_bill_records_no_erp_void(realdb):
    """A mock push records ``MOCK_BILL_REALM`` (§538), but nothing is ever
    written back to the mock, so a void has nothing to undo there: no pending
    marker that no sweep could ever clear."""
    from app.services.erp_link_realm import MOCK_BILL_REALM

    await _configure(realdb)
    _, inv_id, pay_id = await _seed(realdb, realm=MOCK_BILL_REALM)
    with patch("app.services.erp_payment_writeback.dispatch_void_writeback") as dispatched:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(f"/api/payments/{pay_id}/void", json={"reason": "wrong vendor"})
    assert resp.status_code == 200, resp.text
    dispatched.assert_not_called()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_VOIDS_KEY not in instance.state_data


async def test_an_accepted_short_settlement_writes_back_what_was_settled(realdb):
    await _configure(realdb)
    _, inv_id, pay_id = await _seed(realdb)
    async with realdb.sessionmaker("a")() as s:
        await s.execute(
            Payment.__table__.update()
            .where(Payment.id == pay_id)
            .values(settled_amount=Decimal("95.00"), settled_currency="USD")
        )
        await s.execute(
            Invoice.__table__.update().where(Invoice.id == inv_id).values(status="paid")
        )
        await s.commit()
    adapter = FakeQbo()
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.write_back_settled_payment(
            realdb.info("a").org_id, pay_id
        )
    assert outcome == erp_payment_writeback.WROTE
    (payload,) = adapter.post_payment.await_args.args
    assert payload.amount == Decimal("95.00")  # what reached the supplier


# ---------------------------------------------------------------------------
# a write-back flag is deduplicated by its kind, never by its type (§535)
# ---------------------------------------------------------------------------


async def _live_row(realdb, inv_id, kind: str) -> uuid.UUID:
    """A live `erp_reconciliation` on the invoice that some OTHER report opened."""
    exc_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            APException(
                id=exc_id,
                organization_id=realdb.info("a").org_id,
                invoice_id=inv_id,
                exception_type="erp_reconciliation",
                severity="error",
                status="open",
                description="Another report.",
                erp_report={"kind": kind, "erp_type": "quickbooks_online"},
            )
        )
        await s.commit()
    return exc_id


def _kinds(rows) -> list[str]:
    return sorted(r.erp_report["kind"] for r in rows)


async def test_a_failed_write_back_is_not_swallowed_by_an_unrelated_live_row(realdb):
    """A failed write-back fires once per payment: swallowed by a leftover
    void write-back flag, QuickBooks would still show the bill open and nobody
    would be told."""
    await _configure(realdb)
    run_id, inv_id, _ = await _seed(realdb)
    await _live_row(realdb, inv_id, "payment_void_writeback_failed")

    await _sync(realdb, run_id, FakeQbo(ErpPostResult(success=False, message="x")))

    assert _kinds(await _rows(realdb, APException, invoice_id=inv_id)) == [
        "payment_void_writeback_failed",
        "payment_writeback_failed",
    ]


async def test_a_repeated_write_back_failure_is_flagged_once(realdb):
    await _configure(realdb)
    run_id, inv_id, _ = await _seed(realdb)
    adapter = FakeQbo(ErpPostResult(success=False, message="x", retryable=True))

    await _sync(realdb, run_id, adapter)
    await _sync(realdb, run_id, adapter)

    assert adapter.post_payment.await_count == 2
    assert _kinds(await _rows(realdb, APException, invoice_id=inv_id)) == [
        "payment_writeback_failed"
    ]


async def test_a_bill_left_open_is_not_swallowed_by_an_unrelated_live_row(realdb):
    await _configure(realdb)
    run_id, inv_id, _ = await _seed(realdb)
    await _live_row(realdb, inv_id, "bill_state")
    left = ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal("2.00"))

    await _sync(realdb, run_id, FakeQbo(left))

    assert _kinds(await _rows(realdb, APException, invoice_id=inv_id)) == [
        "bill_state",
        "payment_writeback_balance_open",
    ]


async def test_a_failed_void_write_back_is_not_swallowed_by_an_unrelated_live_row(realdb):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    await _live_row(realdb, inv_id, "payment_writeback_balance_open")
    adapter.void_payment.return_value = ErpPostResult(success=False, message="x", retryable=True)

    for _ in range(2):  # a repeat of the same failure is flagged once
        with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
            outcome = await erp_payment_writeback.void_written_payment(
                realdb.info("a").org_id, pay_id
            )
        assert outcome == erp_payment_writeback.FAILED

    assert _kinds(await _rows(realdb, APException, invoice_id=inv_id)) == [
        "payment_void_writeback_failed",
        "payment_writeback_balance_open",
    ]


# ---------------------------------------------------------------------------
# The opt-in: the discount booked as a vendor credit applied to the bill (§575)
# ---------------------------------------------------------------------------


class FakeQboWithCredit(FakeQbo):
    supports_discount_credit = True

    def __init__(self, result=None, credit=None):
        super().__init__(
            result
            or ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal("2"))
        )
        self.post_discount_credit = AsyncMock(
            return_value=credit
            or ErpPostResult(success=True, erp_document_id="VC-1", bill_balance_after=Decimal(0))
        )
        self.void_discount_credit = AsyncMock(
            return_value=ErpPostResult(success=True, erp_document_id="VC-1")
        )
        self.find_discount_credit = AsyncMock(return_value="VC-1")


DISCOUNT_ON = {"book_discount_as_credit": "on", "discount_credit_account": "4900"}


async def _discounted(realdb, **erp_extra):
    await _configure(realdb, **erp_extra)
    run_id, inv_id, pay_id = await _seed(realdb)
    async with realdb.sessionmaker("a")() as s:
        payment = await s.get(Payment, pay_id)
        payment.amount = Decimal("98.00")
        payment.discount_amount = Decimal("2.00")
        s.add(
            GLAccount(
                organization_id=realdb.info("a").org_id,
                code="4900",
                name="Discounts received",
                erp_account_id="88",
                erp_link_realm=REALM,
            )
        )
        await s.commit()
    return run_id, inv_id, pay_id


async def test_with_the_opt_in_the_discount_is_booked_and_the_bill_is_not_flagged(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit()
    await _sync(realdb, run_id, adapter)
    (credit,) = adapter.post_discount_credit.await_args.args
    assert credit.payment_id == str(pay_id) and credit.bill_id == "501"
    assert credit.amount == Decimal("2.00") and credit.currency == "USD"
    # The org's discount account and the payment's own pay-from account, as
    # QuickBooks knows them.
    assert credit.discount_account_erp_id == "88" and credit.pay_from_account_erp_id == "35"
    rows = await _rows(realdb, AuditLog, entity_id=pay_id)
    [posted] = [r for r in rows if r.action == "payment.erp_discount_credit_posted"]
    assert posted.details["discount_amount"] == "2.00"
    assert posted.details["erp_document_id"] == "VC-1"
    # The credit closed the bill in the ERP: nothing left for a person.
    assert await _rows(realdb, APException, invoice_id=inv_id) == []


async def test_without_the_opt_in_nothing_changes(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb)
    adapter = FakeQboWithCredit()
    await _sync(realdb, run_id, adapter)
    adapter.post_discount_credit.assert_not_awaited()
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.erp_report["kind"] == "payment_writeback_balance_open"
    assert flag.erp_report["discount_amount"] == "2.00"
    assert "discount_credit_failure" not in flag.erp_report


async def test_a_payment_with_no_discount_books_no_credit(realdb):
    await _configure(realdb, **DISCOUNT_ON)
    run_id, _, _ = await _seed(realdb)
    adapter = FakeQboWithCredit(
        ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal(0))
    )
    await _sync(realdb, run_id, adapter)
    adapter.post_discount_credit.assert_not_awaited()


async def test_a_refused_credit_is_coded_and_leaves_the_flag_for_a_person(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(
        credit=erp_preflight_refusal("quickbooks_online", "currency_mismatch")
    )
    await _sync(realdb, run_id, adapter)
    [invoice] = await _rows(realdb, Invoice, id=inv_id)
    assert invoice.status == InvoiceStatus.paid
    rows = await _rows(realdb, AuditLog, entity_id=pay_id)
    [failed] = [r for r in rows if r.action == "payment.erp_discount_credit_failed"]
    assert failed.details["refusal"]["code"] == "currency_mismatch"
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.erp_report["kind"] == "payment_writeback_balance_open"
    assert flag.erp_report["bill_balance_after"] == "2"
    assert flag.erp_report["discount_credit_failure"] == "currency_mismatch"
    # Refused before any write: no credit can exist, so a person books it.
    assert "discount_credit_pending" not in flag.erp_report
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_CREDITS_KEY not in instance.state_data


async def _org(realdb):
    async with realdb.control_sessionmaker()() as c:
        return await c.get(Organization, realdb.info("a").org_id)


async def test_a_credit_created_but_not_applied_is_recorded_and_applied_by_the_sweep(realdb):
    """Decisions §577: the credit stands in the ERP unapplied. The flag names it
    and says a retry applies it; the sweep applies THAT credit (by id) through
    the adapter, and the marker clears."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(
        credit=ErpPostResult(
            success=False,
            erp_document_id="VC-7",
            message="quickbooks_online post failed: vendor_credit_unconfirmed",
        )
    )
    await _sync(realdb, run_id, adapter)
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.erp_report["discount_credit_pending"] is True
    assert flag.erp_report["discount_credit_document_id"] == "VC-7"
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    marker = instance.state_data[erp_payment_writeback.PENDING_CREDITS_KEY][str(pay_id)]
    assert marker["credit_id"] == "VC-7" and marker["amount"] == "2.00"

    adapter.post_discount_credit.reset_mock()
    adapter.post_discount_credit.return_value = ErpPostResult(
        success=True, erp_document_id="VC-7", bill_balance_after=Decimal(0)
    )
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        left = await erp_payment_writeback.resume_pending_credits(await _org(realdb))
    assert left == 0
    (retry,) = adapter.post_discount_credit.await_args.args
    assert retry.credit_id == "VC-7" and retry.amount == Decimal("2.00")
    assert retry.discount_account_erp_id == "88" and retry.bill_id == "501"
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_CREDITS_KEY not in instance.state_data
    rows = await _rows(realdb, AuditLog, entity_id=pay_id)
    assert any(
        r.action == "payment.erp_discount_credit_posted" and r.details.get("retry") for r in rows
    )


async def test_the_sweep_leaves_a_voided_payments_credit_to_its_void(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(
        credit=ErpPostResult(success=False, erp_document_id="VC-7", message="x")
    )
    await _sync(realdb, run_id, adapter)
    async with realdb.sessionmaker("a")() as s:
        (await s.get(Payment, pay_id)).status = "voided"
        await s.commit()
    adapter.post_discount_credit.reset_mock()
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        await erp_payment_writeback.resume_pending_credits(await _org(realdb))
    adapter.post_discount_credit.assert_not_awaited()
    # The void deletes it by the recorded id and clears the marker.
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.WROTE
    (voided,) = adapter.void_discount_credit.await_args.args
    assert voided.credit_id == "VC-7"
    adapter.find_discount_credit.assert_not_awaited()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_CREDITS_KEY not in instance.state_data


async def test_an_unlinked_discount_account_reaches_the_adapter_as_unmapped(realdb):
    run_id, _, _ = await _discounted(
        realdb, book_discount_as_credit="on", discount_credit_account="4999"
    )
    adapter = FakeQboWithCredit()
    await _sync(realdb, run_id, adapter)
    (credit,) = adapter.post_discount_credit.await_args.args
    assert credit.discount_account_erp_id is None


async def test_a_failed_payment_write_back_books_no_credit(realdb):
    run_id, _, _ = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(erp_refusal("quickbooks_online", "bill_not_in_erp"))
    await _sync(realdb, run_id, adapter)
    adapter.post_discount_credit.assert_not_awaited()


async def test_a_void_reverses_our_credit_before_our_payment(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit()
    await _sync(realdb, run_id, adapter)
    order: list[str] = []
    adapter.void_discount_credit.side_effect = lambda p: (
        order.append("credit") or ErpPostResult(success=True, erp_document_id="VC-1")
    )
    adapter.void_payment.side_effect = lambda p: (
        order.append("payment") or ErpPostResult(success=True, erp_document_id="BP-9")
    )
    org_id = realdb.info("a").org_id
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(org_id, pay_id)
    assert outcome == erp_payment_writeback.WROTE and order == ["credit", "payment"]
    (credit,) = adapter.void_discount_credit.await_args.args
    assert credit.payment_id == str(pay_id) and credit.bill_id == "501"
    actions = {r.action for r in await _rows(realdb, AuditLog, entity_id=pay_id)}
    assert {"payment.erp_discount_credit_reversed", "payment.erp_voided"} <= actions


async def test_a_credit_that_cannot_be_reversed_stops_the_void_and_is_flagged(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit()
    await _sync(realdb, run_id, adapter)
    adapter.void_discount_credit.return_value = ErpPostResult(
        success=False, message="quickbooks_online void failed: network_error"
    )
    org_id = realdb.info("a").org_id
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(org_id, pay_id)
    assert outcome == erp_payment_writeback.FAILED
    adapter.void_payment.assert_not_awaited()
    rows = await _rows(realdb, AuditLog, entity_id=pay_id)
    [failed] = [r for r in rows if r.action == "payment.erp_void_failed"]
    assert failed.details["document"] == "discount_credit"
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.erp_report["kind"] == "payment_void_writeback_failed"
    assert flag.erp_report["discount_credit_document_id"] == "VC-1"


async def test_a_void_stopped_on_the_credit_retries_by_the_recorded_id(realdb):
    """Decisions §577: the id is recorded before anything is deleted, so a
    retry after the application went (the bill no longer leads to the credit)
    still deletes the credit rather than report nothing to do."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit()
    await _sync(realdb, run_id, adapter)
    adapter.void_discount_credit.return_value = ErpPostResult(
        success=False, message="quickbooks_online void failed: http_500"
    )
    org_id = realdb.info("a").org_id
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        first = await erp_payment_writeback.void_written_payment(org_id, pay_id)
    assert first == erp_payment_writeback.FAILED
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    # The id the write-back booked is the one recorded (decisions §582): the
    # void used it and wrote no second copy.
    recorded = instance.state_data[erp_payment_writeback.BOOKED_CREDITS_KEY]
    assert recorded == {str(pay_id): "VC-1"}

    adapter.find_discount_credit.reset_mock()
    adapter.find_discount_credit.return_value = None  # the application is gone
    adapter.void_discount_credit.return_value = ErpPostResult(success=True, erp_document_id="VC-1")
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        again = await erp_payment_writeback.void_written_payment(org_id, pay_id)
    assert again == erp_payment_writeback.WROTE
    (retry,) = adapter.void_discount_credit.await_args.args
    assert retry.credit_id == "VC-1"
    adapter.find_discount_credit.assert_not_awaited()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_VOID_CREDITS_KEY not in instance.state_data
    assert erp_payment_writeback.BOOKED_CREDITS_KEY not in instance.state_data


async def test_a_void_stopped_on_a_found_credit_records_its_id_before_deleting(realdb):
    """Decisions §577: with no id known, the one the lookup found is recorded
    before anything is deleted."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied(None))
    await _sync(realdb, run_id, adapter)
    async with realdb.sessionmaker("a")() as s:
        (await s.get(Payment, pay_id)).status = "voided"
        await s.commit()
    adapter.find_discount_credit.return_value = "VC-1"
    adapter.void_discount_credit.return_value = ErpPostResult(
        success=False, message="quickbooks_online void failed: http_500"
    )
    org_id = realdb.info("a").org_id
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        assert (
            await erp_payment_writeback.void_written_payment(org_id, pay_id)
            == erp_payment_writeback.FAILED
        )
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    recorded = instance.state_data[erp_payment_writeback.PENDING_VOID_CREDITS_KEY]
    assert recorded == {str(pay_id): "VC-1"}


# ---------------------------------------------------------------------------
# A recorded credit: re-runs, unknown ids, the void race, retries (§579)
# ---------------------------------------------------------------------------


def _unapplied(credit_id: str | None = "VC-7") -> ErpPostResult:
    return ErpPostResult(
        success=False,
        erp_document_id=credit_id,
        message="quickbooks_online post failed: vendor_credit_unconfirmed",
    )


async def _record(realdb, inv_id, pay_id) -> dict | None:
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    return (instance.state_data.get(erp_payment_writeback.PENDING_CREDITS_KEY) or {}).get(
        str(pay_id)
    )


async def _rerun(realdb, pay_id, adapter):
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        return await erp_payment_writeback.write_back_settled_payment(
            realdb.info("a").org_id, pay_id
        )


async def _sweep(realdb, adapter) -> int:
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        return await erp_payment_writeback.resume_pending_credits(await _org(realdb))


def _actions(rows, action: str) -> list:
    return [r for r in rows if r.action == action]


async def test_a_write_back_rerun_applies_the_recorded_credit_and_books_no_second(realdb):
    """A manual `/sync-erp` or webhook re-dispatch of an already-paid invoice
    finishes the credit the first attempt recorded (VC-7), by its id and its
    recorded amount, rather than create another (VC-8)."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] == "VC-7"

    adapter.post_discount_credit.reset_mock()
    adapter.post_discount_credit.return_value = ErpPostResult(
        success=True, erp_document_id="VC-7", bill_balance_after=Decimal(0)
    )
    assert await _rerun(realdb, pay_id, adapter) == erp_payment_writeback.WROTE
    (retry,) = adapter.post_discount_credit.await_args.args
    # Applied by id: the adapter never reaches its create with an id in hand.
    assert retry.credit_id == "VC-7" and retry.amount == Decimal("2.00")
    assert retry.discount_account_erp_id == "88"
    assert await _record(realdb, inv_id, pay_id) is None


async def test_a_pre_write_refusal_on_a_rerun_keeps_the_record(realdb):
    """VC-7 still stands whatever this attempt refused before writing; dropping
    the record would leave it untracked for the sweep and the void."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)
    refused = erp_preflight_refusal("quickbooks_online", "payment_exceeds_balance")
    adapter.post_discount_credit.return_value = refused
    await _rerun(realdb, pay_id, adapter)
    record = await _record(realdb, inv_id, pay_id)
    assert record["credit_id"] == "VC-7" and record["attempts"] == 2
    assert record["outcome"] == "payment_exceeds_balance"
    # The sweep's pass refuses the same way: also kept.
    await _sweep(realdb, adapter)
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] == "VC-7"


async def test_a_record_with_no_known_id_is_never_created_again(realdb):
    """The create's answer was lost: a credit may stand that we cannot name.
    The sweep looks it up and, not finding it, holds the record and tells a
    person — it never calls the create again on an unconfirmed key replay."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied(None))
    await _sync(realdb, run_id, adapter)
    record = await _record(realdb, inv_id, pay_id)
    assert record is not None and record["credit_id"] is None
    [stalled] = [
        r
        for r in await _rows(realdb, APException, invoice_id=inv_id)
        if r.erp_report["kind"] == erp_payment_writeback.ERP_REPORT_DISCOUNT_CREDIT_STALLED
    ]
    assert stalled.erp_report["reason"] == "unidentified"
    assert stalled.erp_report["discount_credit_marker"] == f"FeohLedger discount {pay_id}"
    assert stalled.description_code == "exception.erp_discount_credit_stalled"

    adapter.post_discount_credit.reset_mock()
    adapter.find_discount_credit.return_value = None
    assert await _sweep(realdb, adapter) == 1
    adapter.post_discount_credit.assert_not_awaited()
    adapter.find_discount_credit.assert_awaited_once()
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] is None
    flags = await _rows(realdb, APException, invoice_id=inv_id)
    assert _kinds(flags).count(erp_payment_writeback.ERP_REPORT_DISCOUNT_CREDIT_STALLED) == 1

    # Found by a positive match on a later pass: applied by THAT id.
    adapter.find_discount_credit.return_value = "VC-9"
    adapter.post_discount_credit.return_value = ErpPostResult(
        success=True, erp_document_id="VC-9", bill_balance_after=Decimal(0)
    )
    assert await _sweep(realdb, adapter) == 0
    (applied,) = adapter.post_discount_credit.await_args.args
    assert applied.credit_id == "VC-9"
    assert await _record(realdb, inv_id, pay_id) is None


async def test_the_void_does_not_report_success_over_an_unidentified_credit(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied(None))
    await _sync(realdb, run_id, adapter)
    async with realdb.sessionmaker("a")() as s:
        (await s.get(Payment, pay_id)).status = "voided"
        await s.commit()
    adapter.find_discount_credit.return_value = None
    adapter.void_discount_credit.return_value = ErpPostResult(
        success=True, message="No discount credit of ours"
    )
    org_id = realdb.info("a").org_id
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(org_id, pay_id)
    assert outcome == erp_payment_writeback.FAILED
    # Our payment is voided; the credit we cannot name stays recorded, flagged.
    adapter.void_payment.assert_awaited_once()
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] is None
    flags = await _rows(realdb, APException, invoice_id=inv_id)
    assert erp_payment_writeback.ERP_REPORT_DISCOUNT_CREDIT_STALLED in _kinds(flags)


async def test_a_void_committed_during_the_sweeps_call_re_arms_the_void_with_the_credit(realdb):
    """Sweep reads the payment `completed`, calls the ERP; a void commits (and
    its reversal runs) meanwhile; the ERP applies the credit. The outcome,
    written under the instance lock, re-reads the payment and hands the credit
    to the void instead of leaving it applied to a voided payment's bill."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)

    async def void_lands_meanwhile(payload):
        async with realdb.sessionmaker("a")() as s:
            payment = await s.get(Payment, pay_id)
            invoice = await s.get(Invoice, inv_id)
            payment.status = "voided"
            assert await erp_payment_writeback.record_pending_erp_void(
                s, invoice=invoice, payment=payment
            )
            await s.commit()
        return ErpPostResult(success=True, erp_document_id="VC-7", bill_balance_after=Decimal(0))

    adapter.post_discount_credit.side_effect = void_lands_meanwhile
    await _sweep(realdb, adapter)
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    state = instance.state_data
    assert erp_payment_writeback.PENDING_CREDITS_KEY not in state
    assert state[erp_payment_writeback.PENDING_VOID_CREDITS_KEY] == {str(pay_id): "VC-7"}
    assert str(pay_id) in state[erp_payment_writeback.PENDING_VOIDS_KEY]

    adapter.find_discount_credit.return_value = None
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.WROTE
    (reversed_,) = adapter.void_discount_credit.await_args.args
    assert reversed_.credit_id == "VC-7"


async def test_a_void_finishing_meanwhile_leaves_a_re_armed_credit_pending(realdb):
    """The void's own clear is compare-then-clear: a credit re-armed while it
    was calling the ERP is not wiped with the credit it handled."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit()
    await _sync(realdb, run_id, adapter)
    async with realdb.sessionmaker("a")() as s:
        payment = await s.get(Payment, pay_id)
        payment.status = "voided"
        await erp_payment_writeback.record_pending_erp_void(
            s, invoice=await s.get(Invoice, inv_id), payment=payment
        )
        await s.commit()
    adapter.find_discount_credit.return_value = None

    async def credit_re_armed_meanwhile(payload):
        async with realdb.sessionmaker("a")() as s:
            await erp_payment_writeback._set_map_entry(
                s, inv_id, erp_payment_writeback.PENDING_VOID_CREDITS_KEY, pay_id, "VC-8"
            )
            await s.commit()
        return ErpPostResult(success=True, erp_document_id="BP-9")

    adapter.void_payment.side_effect = credit_re_armed_meanwhile
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.PENDING_VOID_CREDITS_KEY] == {
        str(pay_id): "VC-8"
    }
    assert str(pay_id) in instance.state_data[erp_payment_writeback.PENDING_VOIDS_KEY]


async def test_a_record_claimed_by_one_pass_is_not_processed_by_another(realdb):
    """The tick and `sync_org_now` at once: the second finds the first's claim
    and leaves the record alone, so one credit sees one ERP attempt."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)
    adapter.post_discount_credit.reset_mock()
    inner: list[int] = []

    async def second_pass_meanwhile(payload):
        inner.append(await erp_payment_writeback.resume_pending_credits(await _org(realdb)))
        return ErpPostResult(success=True, erp_document_id="VC-7", bill_balance_after=Decimal(0))

    adapter.post_discount_credit.side_effect = second_pass_meanwhile
    assert await _sweep(realdb, adapter) == 0
    assert adapter.post_discount_credit.await_count == 1
    assert inner == [1]  # it saw the record, claimed by the first, and skipped it
    assert await _record(realdb, inv_id, pay_id) is None


async def test_a_repeated_failure_is_audited_once_and_stops_at_the_cap(realdb, monkeypatch):
    monkeypatch.setattr(erp_payment_writeback, "MAX_CREDIT_ATTEMPTS", 3)
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)  # attempt 1
    adapter.post_discount_credit.reset_mock()
    await _sweep(realdb, adapter)  # attempt 2: same outcome
    rows = await _rows(realdb, AuditLog, entity_id=pay_id)
    assert len(_actions(rows, "payment.erp_discount_credit_failed")) == 1
    record = await _record(realdb, inv_id, pay_id)
    assert record["attempts"] == 2 and record["last_attempt"]

    await _sweep(realdb, adapter)  # attempt 3: the cap
    record = await _record(realdb, inv_id, pay_id)
    assert record["exhausted"] is True and record["credit_id"] == "VC-7"
    rows = await _rows(realdb, AuditLog, entity_id=pay_id)
    failed = _actions(rows, "payment.erp_discount_credit_failed")
    assert len(failed) == 2 and any(r.details.get("retries_exhausted") for r in failed)
    [stalled] = [
        r
        for r in await _rows(realdb, APException, invoice_id=inv_id)
        if r.erp_report["kind"] == erp_payment_writeback.ERP_REPORT_DISCOUNT_CREDIT_STALLED
    ]
    assert stalled.erp_report["reason"] == "retries_exhausted"
    assert stalled.erp_report["discount_credit_document_id"] == "VC-7"

    await _sweep(realdb, adapter)  # past the cap: nothing calls the ERP
    assert adapter.post_discount_credit.await_count == 2
    rows = await _rows(realdb, AuditLog, entity_id=pay_id)
    assert len(_actions(rows, "payment.erp_discount_credit_failed")) == 2
    # The void still reverses it by the recorded id.
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] == "VC-7"


# ---------------------------------------------------------------------------
# A re-run takes the record it CLAIMED; a first attempt reserves before it
# creates; the write-back locks the invoice before the instance (§581)
# ---------------------------------------------------------------------------


async def _set_record(realdb, inv_id, pay_id, **fields) -> None:
    async with realdb.sessionmaker("a")() as s:
        instance = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
        state = dict(instance.state_data)
        records = dict(state[erp_payment_writeback.PENDING_CREDITS_KEY])
        records[str(pay_id)] = {**records[str(pay_id)], **fields}
        instance.state_data = {**state, erp_payment_writeback.PENDING_CREDITS_KEY: records}
        await s.commit()


def _future() -> str:
    from datetime import timedelta

    return (datetime.now(UTC) + timedelta(minutes=5)).isoformat()


def _past() -> str:
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(minutes=5)).isoformat()


async def test_a_rerun_under_a_live_claim_makes_no_credit_call(realdb):
    """`_claim_record` answers a tuple, so the old `is not None` test was
    always true: a re-run attempted a record another process had claimed."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)
    await _set_record(realdb, inv_id, pay_id, claimed_until=_future())
    adapter.post_discount_credit.reset_mock()
    assert await _rerun(realdb, pay_id, adapter) == erp_payment_writeback.WROTE
    adapter.post_discount_credit.assert_not_awaited()
    adapter.find_discount_credit.assert_not_awaited()
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] == "VC-7"


async def test_a_rerun_after_the_cap_makes_no_credit_call(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)
    await _set_record(realdb, inv_id, pay_id, exhausted=True)
    adapter.post_discount_credit.reset_mock()
    await _rerun(realdb, pay_id, adapter)
    adapter.post_discount_credit.assert_not_awaited()
    assert (await _record(realdb, inv_id, pay_id))["exhausted"] is True


async def test_a_rerun_after_the_sweep_posted_does_not_revive_the_record(realdb):
    """The re-run read the record unlocked; the sweep applied it while our
    payment was being written. The re-run's claim finds nothing, so it makes
    no call and writes no record back from its stale read."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)
    adapter.post_discount_credit.reset_mock()
    adapter.post_discount_credit.return_value = ErpPostResult(
        success=True, erp_document_id="VC-7", bill_balance_after=Decimal(0)
    )
    written = ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal(0))

    async def sweep_posts_meanwhile(payload):
        assert await _sweep(realdb, adapter) == 0
        return written

    adapter.post_payment.side_effect = sweep_posts_meanwhile
    assert await _rerun(realdb, pay_id, adapter) == erp_payment_writeback.WROTE
    assert adapter.post_discount_credit.await_count == 1  # the sweep's, only
    assert await _record(realdb, inv_id, pay_id) is None


async def test_the_first_credit_is_reserved_before_the_create(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit()
    seen: list = []

    async def create(payload):
        seen.append(await _record(realdb, inv_id, pay_id))
        return ErpPostResult(success=True, erp_document_id="VC-1", bill_balance_after=Decimal(0))

    adapter.post_discount_credit.side_effect = create
    await _sync(realdb, run_id, adapter)
    [reservation] = seen
    assert reservation["credit_id"] is None and reservation["attempts"] == 0
    assert reservation["claimed_until"] and reservation["amount"] == "2.00"
    assert await _record(realdb, inv_id, pay_id) is None  # posted: dropped


async def test_a_lost_first_recording_leaves_a_record_that_is_never_created_again(realdb):
    """The create made VC-7 unapplied, then recording the outcome failed (the
    transaction rolled back; a crash is the same). Before §581 nothing was
    recorded and the next attempt created a second credit. Now the
    reservation stands: the re-run only looks the credit up, and a miss is
    held for a person."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied("VC-7"))
    with patch.object(
        erp_payment_writeback,
        "_record_credit_outcome",
        AsyncMock(side_effect=RuntimeError("recording failed")),
    ):
        await _sync(realdb, run_id, adapter)
    assert adapter.post_discount_credit.await_count == 1
    record = await _record(realdb, inv_id, pay_id)
    assert record is not None and record["credit_id"] is None

    # While the dead attempt's claim is live, nothing touches it.
    adapter.post_discount_credit.reset_mock()
    await _rerun(realdb, pay_id, adapter)
    adapter.post_discount_credit.assert_not_awaited()
    adapter.find_discount_credit.assert_not_awaited()

    # Once it lapses, the re-run looks it up — never creates.
    await _set_record(realdb, inv_id, pay_id, claimed_until=_past())
    adapter.find_discount_credit.return_value = None
    await _rerun(realdb, pay_id, adapter)
    adapter.post_discount_credit.assert_not_awaited()
    adapter.find_discount_credit.assert_awaited_once()
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] is None
    flags = await _rows(realdb, APException, invoice_id=inv_id)
    [stalled] = [
        r
        for r in flags
        if r.erp_report["kind"] == erp_payment_writeback.ERP_REPORT_DISCOUNT_CREDIT_STALLED
    ]
    assert stalled.erp_report["reason"] == "unidentified"


async def test_a_void_during_the_payment_write_is_re_armed(realdb):
    """The void commits after the write-back's status check and its ERP void
    runs before our BillPayment exists (nothing to void). The write-back,
    under the locks after the write, sees the payment voided and puts the
    void back for the sweep (decisions §581)."""
    run_id, inv_id, pay_id = await _seed(realdb)
    await _configure(realdb)
    adapter = FakeQbo()

    async def void_lands_and_finishes_meanwhile(payload):
        async with realdb.sessionmaker("a")() as s:
            payment = await s.get(Payment, pay_id)
            payment.status = "voided"
            assert await erp_payment_writeback.record_pending_erp_void(
                s, invoice=await s.get(Invoice, inv_id), payment=payment
            )
            await s.commit()
        async with realdb.sessionmaker("a")() as s:
            await erp_payment_writeback._clear_pending(s, inv_id, pay_id)
            await s.commit()
        return ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal(0))

    async with realdb.sessionmaker("a")() as s:
        (await s.get(Invoice, inv_id)).status = InvoiceStatus.paid
        await s.commit()
    adapter.post_payment.side_effect = void_lands_and_finishes_meanwhile
    assert await _rerun(realdb, pay_id, adapter) == erp_payment_writeback.WROTE
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.PENDING_VOIDS_KEY] == [str(pay_id)]
    [posted] = _actions(await _rows(realdb, AuditLog, entity_id=pay_id), "payment.erp_posted")
    assert posted.details["void_rearmed"] is True


async def test_the_write_back_waits_for_a_door_holding_the_invoice_no_deadlock(realdb):
    """The void door locks the invoice FOR UPDATE, then the instance. The
    write-back's outcome now takes the invoice (FOR KEY SHARE) before the
    instance, so it waits behind the door instead of holding the instance
    the door needs next — a deadlock that used to kill the sweep's side."""
    import asyncio

    from sqlalchemy import text

    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied())
    await _sync(realdb, run_id, adapter)
    record = await _record(realdb, inv_id, pay_id)
    mk = realdb.sessionmaker("a")
    payload = erp_payment_writeback._payload_from_marker(str(pay_id), record)
    applied = ErpPostResult(success=True, erp_document_id="VC-7", bill_balance_after=Decimal(0))

    async def blocked(pid: int) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while loop.time() < deadline:
            async with mk() as probe:
                waiting = (
                    await probe.execute(
                        text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": pid},
                    )
                ).scalar_one_or_none()
            if waiting == "Lock":
                return True
            await asyncio.sleep(0.02)
        return False

    async with mk() as door, mk() as sweep:
        invoice = await sweep.get(Invoice, inv_id)
        await sweep.commit()
        sweep_pid = (await sweep.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        locked = (
            await door.execute(select(Invoice).where(Invoice.id == inv_id).with_for_update())
        ).scalar_one()
        outcome = asyncio.create_task(
            erp_payment_writeback._record_credit_outcome(
                sweep, adapter, invoice, pay_id, payload, applied, prior=record, held=False
            )
        )
        try:
            assert await blocked(sweep_pid), "the outcome did not wait for the invoice"
            payment = await door.get(Payment, pay_id)
            payment.status = "voided"
            await asyncio.wait_for(
                erp_payment_writeback.record_pending_erp_void(
                    door, invoice=locked, payment=payment
                ),
                timeout=10,
            )
            await door.commit()
            await asyncio.wait_for(outcome, timeout=30)
            await sweep.commit()
        finally:
            if not outcome.done():
                outcome.cancel()
    # The outcome ran after the void and handed it the credit it applied.
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.PENDING_VOID_CREDITS_KEY] == {
        str(pay_id): "VC-7"
    }
    assert erp_payment_writeback.PENDING_CREDITS_KEY not in instance.state_data


# ---------------------------------------------------------------------------
# A booked credit is reversed by its id; a re-armed void survives the void's
# own clear; a recorded credit that is not ours is flagged (§582)
# ---------------------------------------------------------------------------


class FakeXeroWithCredit(FakeQboWithCredit):
    """Xero's void as §581 shipped it: a recorded id is voided by id; with
    none it looks by number, which here finds nothing (the lookup Xero's
    published spec does not confirm), and reports "nothing of ours"."""

    erp_type = "xero"

    def __init__(self):
        super().__init__(
            credit=ErpPostResult(
                success=True, erp_document_id="CN-1", bill_balance_after=Decimal(0)
            )
        )
        self.voided: list[str] = []
        self.find_discount_credit = AsyncMock(return_value=None)

        async def void(payload):
            if payload.credit_id:
                self.voided.append(payload.credit_id)
                return ErpPostResult(success=True, erp_document_id=payload.credit_id)
            return ErpPostResult(success=True, message="No discount credit of ours")

        self.void_discount_credit = AsyncMock(side_effect=void)


async def _void_payment(realdb, inv_id, pay_id) -> None:
    async with realdb.sessionmaker("a")() as s:
        payment = await s.get(Payment, pay_id)
        payment.status = "voided"
        assert await erp_payment_writeback.record_pending_erp_void(
            s, invoice=await s.get(Invoice, inv_id), payment=payment
        )
        await s.commit()


async def test_a_booked_credit_is_voided_by_its_id_when_the_number_lookup_misses(realdb):
    """Before §582 a posted credit's id was dropped with its record; the void
    then fell back to Xero's number lookup, found nothing, voided our payment
    and reported success with CN-1 still allocated to the bill."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeXeroWithCredit()
    await _sync(realdb, run_id, adapter)
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.BOOKED_CREDITS_KEY] == {str(pay_id): "CN-1"}
    assert erp_payment_writeback.PENDING_CREDITS_KEY not in instance.state_data

    await _void_payment(realdb, inv_id, pay_id)
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.WROTE
    assert adapter.voided == ["CN-1"]
    adapter.find_discount_credit.assert_not_awaited()
    [reversed_] = _actions(
        await _rows(realdb, AuditLog, entity_id=pay_id), "payment.erp_discount_credit_reversed"
    )
    assert reversed_.details["erp_document_id"] == "CN-1"
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    for key in (
        erp_payment_writeback.BOOKED_CREDITS_KEY,
        erp_payment_writeback.PENDING_VOIDS_KEY,
        erp_payment_writeback.PENDING_VOID_CREDITS_KEY,
    ):
        assert key not in instance.state_data


async def test_a_rerun_after_the_credit_was_booked_books_no_second(realdb):
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeXeroWithCredit()
    await _sync(realdb, run_id, adapter)
    assert adapter.post_discount_credit.await_count == 1
    assert await _rerun(realdb, pay_id, adapter) == erp_payment_writeback.WROTE
    assert adapter.post_discount_credit.await_count == 1
    assert await _record(realdb, inv_id, pay_id) is None


async def test_a_write_back_re_arming_during_the_voids_call_keeps_the_void_pending(realdb):
    """The void's ERP call runs before our BillPayment exists (nothing to
    void); the write-back's outcome then commits its re-arm, usually over an
    id still pending; then the void takes its final lock. Before §582 the
    void cleared the id it had never seen re-armed and BP-9 stood on a voided
    payment's bill. The token makes the re-arm visible: the void stays
    pending, and the next pass voids BP-9."""
    run_id, inv_id, pay_id = await _seed(realdb)
    await _configure(realdb)
    adapter = FakeQbo()
    await _void_payment(realdb, inv_id, pay_id)

    async def write_back_outcome_lands_meanwhile(payload):
        async with realdb.sessionmaker("a")() as s:
            assert await erp_payment_writeback._rearm_void_if_voided(s, inv_id, pay_id)
            await s.commit()
        return ErpPostResult(success=True, message="No bill payment of ours to void")

    adapter.void_payment.side_effect = write_back_outcome_lands_meanwhile
    org_id = realdb.info("a").org_id
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        await erp_payment_writeback.void_written_payment(org_id, pay_id)
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.PENDING_VOIDS_KEY] == [str(pay_id)]

    # The next pass finds BP-9 and voids it; the void and its token clear.
    adapter.void_payment.side_effect = None
    adapter.void_payment.return_value = ErpPostResult(success=True, erp_document_id="BP-9")
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        assert await erp_payment_writeback.resume_pending_voids(await _org(realdb)) == 0
    assert adapter.void_payment.await_count == 2
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_VOIDS_KEY not in instance.state_data
    assert erp_payment_writeback.VOID_REARMED_KEY not in instance.state_data
    voided = _actions(await _rows(realdb, AuditLog, entity_id=pay_id), "payment.erp_voided")
    assert [r.details["erp_document_id"] for r in voided] == ["BP-9"]


async def test_a_recorded_credit_that_is_not_ours_is_held_and_flagged(realdb):
    """§581's residual: a recorded note that reads back without our number
    was refused ``bill_payment_voided`` and its record dropped silently. The
    adapter now fails with the id; the record is held and a person told."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied("CN-7"))
    await _sync(realdb, run_id, adapter)
    adapter.post_discount_credit.return_value = ErpPostResult(
        success=False,
        erp_document_id="CN-7",
        message="Xero post failed: discount_credit_not_ours",
    )
    assert await _sweep(realdb, adapter) == 1
    record = await _record(realdb, inv_id, pay_id)
    assert record["credit_id"] == "CN-7"
    [stalled] = [
        r
        for r in await _rows(realdb, APException, invoice_id=inv_id)
        if r.erp_report["kind"] == erp_payment_writeback.ERP_REPORT_DISCOUNT_CREDIT_STALLED
    ]
    assert stalled.erp_report["reason"] == erp_payment_writeback.DISCOUNT_CREDIT_NOT_OURS
    assert stalled.erp_report["discount_credit_document_id"] == "CN-7"


# ---------------------------------------------------------------------------
# The void reverses only a credit it has an entry for; the stall flag is per
# payment and reason; the booked id is compared before it is cleared (§583)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lookup_raises", [False, True], ids=["lookup_misses", "lookup_raises"])
async def test_a_void_with_no_credit_entry_runs_no_credit_lookup(realdb, lookup_raises):
    """Since §581 a credit of ours always has an entry in one of the three
    maps. Before §583 every void on a credit-capable ERP ran the lookup first
    — Xero's by an unconfirmed number filter — so a rejection there stopped
    the void before ``void_payment`` for an org that never opted in."""
    run_id, inv_id, pay_id = await _seed(realdb)
    await _configure(realdb)
    adapter = FakeXeroWithCredit()
    if lookup_raises:
        adapter.find_discount_credit.side_effect = RuntimeError("filter rejected")
    await _void_payment(realdb, inv_id, pay_id)
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.WROTE
    adapter.find_discount_credit.assert_not_awaited()
    adapter.void_discount_credit.assert_not_awaited()
    adapter.void_payment.assert_awaited_once()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_VOIDS_KEY not in instance.state_data


async def test_a_void_with_a_credit_entry_still_looks_the_credit_up(realdb):
    """A reservation with no id yet is an entry: the lookup runs, and a
    lookup that fails stops the void before ``void_payment`` as before."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeQboWithCredit(credit=_unapplied(None))
    await _sync(realdb, run_id, adapter)
    assert (await _record(realdb, inv_id, pay_id))["credit_id"] is None
    await _void_payment(realdb, inv_id, pay_id)
    adapter.find_discount_credit.side_effect = RuntimeError("filter rejected")
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.FAILED
    adapter.find_discount_credit.assert_awaited_once()
    adapter.void_payment.assert_not_awaited()


async def test_a_booked_id_changed_during_the_voids_call_is_kept(realdb):
    """The void reverses CN-1; while it calls the ERP the booked entry becomes
    CN-2. Before §583 the void cleared the booked map unconditionally and
    CN-2's id was lost. Now the entry and the pending void are kept."""
    run_id, inv_id, pay_id = await _discounted(realdb, **DISCOUNT_ON)
    adapter = FakeXeroWithCredit()
    await _sync(realdb, run_id, adapter)
    await _void_payment(realdb, inv_id, pay_id)

    async def void(payload):
        async with realdb.sessionmaker("a")() as s:
            await erp_payment_writeback._set_map_entry(
                s, inv_id, erp_payment_writeback.BOOKED_CREDITS_KEY, pay_id, "CN-2"
            )
            await s.commit()
        return ErpPostResult(success=True, erp_document_id=payload.credit_id)

    adapter.void_discount_credit.side_effect = void
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    (reversed_,) = adapter.void_discount_credit.await_args.args
    assert reversed_.credit_id == "CN-1"
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.BOOKED_CREDITS_KEY] == {str(pay_id): "CN-2"}
    assert instance.state_data[erp_payment_writeback.PENDING_VOIDS_KEY] == [str(pay_id)]


async def test_the_stall_flag_is_one_per_payment_and_reason(realdb):
    """Before §583 the flag deduped by kind per invoice: an open
    "unidentified" flag swallowed a later "not ours" with its id, and one
    payment's flag swallowed another payment's on the same invoice."""
    run_id, inv_id, pay_id = await _seed(realdb)
    other = uuid.uuid4()
    adapter = FakeQbo()
    calls = [
        (pay_id, None, "unidentified"),
        (pay_id, "CN-7", erp_payment_writeback.DISCOUNT_CREDIT_NOT_OURS),
        (other, None, "unidentified"),
        (pay_id, None, "unidentified"),  # the same fact again: deduped
    ]
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, inv_id)
        for payment_id, credit_id, reason in calls:
            await erp_payment_writeback._open_stalled_flag(
                s, adapter, invoice, payment_id, run_id, "501", credit_id, reason=reason
            )
        await s.commit()
    flags = [
        r
        for r in await _rows(realdb, APException, invoice_id=inv_id)
        if r.erp_report["kind"] == erp_payment_writeback.ERP_REPORT_DISCOUNT_CREDIT_STALLED
    ]
    assert sorted(
        (
            r.erp_report["payment_id"],
            r.erp_report["reason"],
            r.erp_report.get("discount_credit_document_id"),
        )
        for r in flags
    ) == sorted(
        [
            (str(pay_id), "unidentified", None),
            (str(pay_id), erp_payment_writeback.DISCOUNT_CREDIT_NOT_OURS, "CN-7"),
            (str(other), "unidentified", None),
        ]
    )


@pytest.mark.parametrize(
    ("erp", "refused"),
    [
        ({}, False),
        ({"book_discount_as_credit": "off"}, False),
        ({"book_discount_as_credit": "on", "discount_credit_account": "4900"}, False),
        ({"book_discount_as_credit": "on"}, True),
        ({"book_discount_as_credit": "on", "discount_credit_account": "  "}, True),
        ({"book_discount_as_credit": "yes", "discount_credit_account": "4900"}, True),
    ],
)
def test_the_opt_in_needs_an_account(erp, refused):
    from app.services.erp_adapters.catalog import discount_credit_refusal

    assert (discount_credit_refusal({"type": "xero", **erp}) is not None) is refused


# ── Saving the opt-in through PATCH /api/organization (decisions §575, §578) ──


async def _add_gl(realdb, code: str, *, erp_id: str | None, realm: str | None = REALM) -> None:
    async with realdb.sessionmaker("a")() as s:
        s.add(
            GLAccount(
                organization_id=realdb.info("a").org_id,
                code=code,
                name=f"Account {code}",
                erp_account_id=erp_id,
                erp_link_realm=realm if erp_id else None,
            )
        )
        await s.commit()


async def _save_erp(realdb, **erp_extra):
    erp = {
        "type": "quickbooks_online",
        "integration_method": "direct",
        "bill_payment_bank_account": "1010",
        "bill_payment_card_account": "2050",
        **erp_extra,
    }
    async with realdb.client(key="a", role="admin") as c:
        return await c.patch("/api/organization", json={"settings": {"erp": erp}})


async def _stored_discount_flag(realdb):
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        return ((org.settings or {}).get("erp") or {}).get("book_discount_as_credit")


@pytest.mark.plan("scale")
async def test_saving_the_opt_in_without_an_account_is_refused(realdb):
    await _configure(realdb)
    resp = await _save_erp(realdb, book_discount_as_credit="on")
    assert resp.status_code == 422, resp.text
    assert "discount_credit_account is required" in resp.json()["detail"]
    assert await _stored_discount_flag(realdb) is None


@pytest.mark.plan("scale")
async def test_saving_the_opt_in_with_a_shared_linked_account_succeeds(realdb):
    await _configure(realdb)
    await _add_gl(realdb, "4910", erp_id="91")
    resp = await _save_erp(realdb, book_discount_as_credit="on", discount_credit_account="4910")
    assert resp.status_code == 200, resp.text
    assert await _stored_discount_flag(realdb) == "on"


@pytest.mark.plan("scale")
@pytest.mark.parametrize(
    "account",
    ["unknown", "never_synced", "other_company", "entity_only"],
)
async def test_saving_the_opt_in_with_an_account_the_write_back_cannot_resolve_is_refused(
    realdb, account
):
    """What `_resolve_account` would answer None for on an org-wide invoice is
    refused at save time, so the form never turns on a credit every write-back
    would refuse (decisions §578)."""
    from app.models.entity import Entity

    await _configure(realdb)
    if account == "never_synced":
        await _add_gl(realdb, "4920", erp_id=None)
    elif account == "other_company":
        await _add_gl(realdb, "4920", erp_id="92", realm="quickbooks_online:other-co")
    elif account == "entity_only":
        async with realdb.sessionmaker("a")() as s:
            entity = (await s.execute(select(Entity).limit(1))).scalar_one()
            s.add(
                GLAccount(
                    organization_id=realdb.info("a").org_id,
                    entity_id=entity.id,
                    code="4920",
                    name="Entity discounts",
                    erp_account_id="93",
                    erp_link_realm=REALM,
                )
            )
            await s.commit()
    resp = await _save_erp(realdb, book_discount_as_credit="on", discount_credit_account="4920")
    assert resp.status_code == 422, resp.text
    assert "organization-wide GL account linked" in resp.json()["detail"]
    assert await _stored_discount_flag(realdb) is None
