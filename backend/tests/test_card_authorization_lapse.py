"""A card whose authorization lapses returns to unspent (decisions §544).

An authorization is a hold, not money moved (§543). When the hold is reversed
or expires, the card webhook takes the card from `charged` back to `active`,
clears the authorized figure and leaves a `card.authorization_lapsed` audit
row, so a later void or reissue — both of which refuse a spent card — can
close it. A decline is a refused attempt and never releases a standing hold;
a reversal on a SETTLED card is a refund / chargeback and leaves the card
untouched. The run's card leg is `in_flight` throughout and no payment sync is
dispatched for a lapse.

Driven through the real signed webhook for each provider. Runs against the
opt-in ``realdb`` fixture.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.payment import Payment
from app.models.virtual_card import CardRebate, VirtualCard
from app.models.workflow import AuditLog
from tests.test_card_money_path_regression import (
    _card_row,
    _leg,
    _seed_invoice,
    _seed_run,
    _set_card_settings,
    _sign,
)

pytestmark = pytest.mark.asyncio

TENANT = "a"


def _body(
    provider: str,
    token: str,
    outcome: str,
    *,
    amount=None,
    event_id=None,
    txn=None,
    events=None,
) -> bytes:
    """A provider's delivery for ``outcome`` (authorization / settlement /
    reversal / expiry / decline), in that provider's own shape. ``txn`` is the
    provider's transaction id (default ``txn_<token>``, one transaction per
    card); ``events`` is Lithic's ordered `events` list."""
    event_id = event_id or str(uuid.uuid4())
    txn = txn or f"txn_{token}"
    if provider == "lithic":
        # `card_transaction.updated` carries the transaction; its `status`
        # names the outcome (https://docs.lithic.com/docs/transactions).
        status = {
            "authorization": "PENDING",
            "settlement": "SETTLED",
            "reversal": "VOIDED",
            "expiry": "EXPIRED",
            "decline": "DECLINED",
        }[outcome]
        payload = {
            "type": "card_transaction.updated",
            "event_id": event_id,
            "token": txn,
            "card_token": token,
            "status": status,
            "merchant": {"descriptor": "Acme Fuel"},
        }
        if amount is not None:
            payload["amount"] = int(Decimal(amount) * 100)  # minor units
        if events is not None:
            payload["events"] = events
    elif provider == "nium":
        names = {
            "authorization": {"eventType": "authorization.approved"},
            "settlement": {"eventType": "transaction.settled"},
            # Nium's documented reversal webhook names itself in `template`.
            "reversal": {"template": "CARD_TRANSACTION_REVERSAL_WEBHOOK"},
            "expiry": {"eventType": "authorization.expired"},
            "decline": {"eventType": "authorization.declined"},
        }
        payload = {
            **names[outcome],
            "webhookId": event_id,
            "cardHashId": token,
            "authCode": txn,
            "merchantName": "Acme Fuel",
        }
        if amount is not None:
            payload["transactionAmount" if outcome == "reversal" else "amount"] = amount
    else:
        from app.services.card_adapters.mock_adapter import (
            MOCK_EVENT_AUTHORIZATION,
            MOCK_EVENT_EXPIRY,
            MOCK_EVENT_REVERSAL,
            MOCK_EVENT_SETTLEMENT,
        )

        event_type = {
            "authorization": MOCK_EVENT_AUTHORIZATION,
            "settlement": MOCK_EVENT_SETTLEMENT,
            "reversal": MOCK_EVENT_REVERSAL,
            "expiry": MOCK_EVENT_EXPIRY,
            "decline": "authorization.declined",
        }[outcome]
        payload = {
            "card_token": token,
            "type": event_type,
            "event_id": event_id,
            "transaction_id": txn,
            "merchant": {"descriptor": "Acme Fuel"},
        }
        if amount is not None:
            payload["amount"] = amount
    return json.dumps(payload).encode("utf-8")


async def _post(realdb, provider: str, body: bytes, *, signature: str | None = None) -> int:
    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            f"/api/cards/webhook/{provider}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Webhook-Signature": signature if signature is not None else _sign(body),
            },
        )
    return resp.status_code


async def _leg_with_card(realdb, provider: str, *, amount=Decimal("100.00")):
    """A run's card leg as execution leaves it: payment `completed`, its card
    issued (`active`) on ``provider`` and linked to it."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    token = f"tok_{uuid.uuid4().hex[:12]}"
    inv = await _seed_invoice(mk, org_id, amount=amount)
    _run_id, pay_id = await _seed_run(mk, org_id, inv, amount=amount, creator_id=None)
    async with mk() as s:
        await s.execute(
            Payment.__table__.update().where(Payment.id == pay_id).values(status="completed")
        )
        card = _card_row(inv, org_id, provider_card_id=token, limit=amount, provider=provider)
        card.payment_id = pay_id
        s.add(card)
        await s.commit()
        card_id = card.id
    await _set_card_settings(realdb, org_id)
    return card_id, pay_id, token


async def _card(realdb, card_id) -> VirtualCard:
    async with realdb.sessionmaker(TENANT)() as s:
        return await s.get(VirtualCard, card_id)


async def _audits(realdb, card_id, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.entity_id == card_id, AuditLog.action == action)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.parametrize("provider", ["lithic", "nium", "mock"])
@pytest.mark.parametrize("lapse", ["reversal", "expiry"])
async def test_a_lapsed_authorization_returns_the_card_to_unspent(realdb, provider, lapse):
    card_id, pay_id, token = await _leg_with_card(realdb, provider)

    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        assert (
            await _post(realdb, provider, _body(provider, token, "authorization", amount="100.00"))
            == 204
        )
        assert (await _card(realdb, card_id)).status == "charged"
        _payment, state = await _leg(realdb, pay_id)
        assert state == "in_flight"
        assert await _post(realdb, provider, _body(provider, token, lapse)) == 204

    card = await _card(realdb, card_id)
    assert card.status == "active"
    assert card.amount_charged is None and card.charged_at is None
    payment, state = await _leg(realdb, pay_id)
    assert payment.settled_amount is None
    assert state == "in_flight"
    dispatched.assert_not_awaited()

    (row,) = await _audits(realdb, card_id, "card.authorization_lapsed")
    assert row.details["from"] == "charged" and row.details["to"] == "active"
    assert row.details["lapse"] == lapse
    assert row.details["released_amount"] == "100.00"  # string Decimal, never float
    assert token not in json.dumps(row.details)


@pytest.mark.parametrize("provider", ["lithic", "nium", "mock"])
async def test_a_decline_never_releases_a_standing_authorization(realdb, provider):
    """A second attempt at a single-use card is declined while the first hold
    stands; nothing ties the decline to that hold, so the card stays charged."""
    card_id, pay_id, token = await _leg_with_card(realdb, provider)
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert (
            await _post(realdb, provider, _body(provider, token, "authorization", amount="100.00"))
            == 204
        )
        assert await _post(realdb, provider, _body(provider, token, "decline")) == 204

    card = await _card(realdb, card_id)
    assert card.status == "charged" and card.amount_charged == Decimal("100.00")
    assert await _audits(realdb, card_id, "card.authorization_lapsed") == []
    _payment, state = await _leg(realdb, pay_id)
    assert state == "in_flight"


async def test_a_replayed_reversal_is_deduped(realdb):
    """The provider's retry of a reversal is a no-op: one audit row, and a
    fresh authorization in between is not undone by the replay."""
    card_id, _pay_id, token = await _leg_with_card(realdb, "nium")
    reversal = _body("nium", token, "reversal", event_id=str(uuid.uuid4()))
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="100.00"))
            == 204
        )
        assert await _post(realdb, "nium", reversal) == 204
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="80.00"))
            == 204
        )
        assert await _post(realdb, "nium", reversal) == 204

    card = await _card(realdb, card_id)
    assert card.status == "charged" and card.amount_charged == Decimal("80.00")
    assert len(await _audits(realdb, card_id, "card.authorization_lapsed")) == 1


async def test_an_unsigned_reversal_changes_nothing(realdb):
    card_id, _pay_id, token = await _leg_with_card(realdb, "nium")
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="100.00"))
            == 204
        )
        body = _body("nium", token, "reversal")
        assert await _post(realdb, "nium", body, signature="0" * 64) == 204

    assert (await _card(realdb, card_id)).status == "charged"
    assert await _audits(realdb, card_id, "card.authorization_lapsed") == []


@pytest.mark.parametrize("provider", ["lithic", "nium", "mock"])
async def test_a_reversal_after_settlement_leaves_the_card_untouched(realdb, provider):
    """Money moved: a reversal now is a refund / chargeback. The card stays
    `completed` with its settled figure, the rebate and the run's recorded
    figure stand, and the event is kept on the trail."""
    card_id, pay_id, token = await _leg_with_card(realdb, provider)
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert (
            await _post(realdb, provider, _body(provider, token, "authorization", amount="100.00"))
            == 204
        )
        assert (
            await _post(realdb, provider, _body(provider, token, "settlement", amount="100.00"))
            == 204
        )
        assert await _post(realdb, provider, _body(provider, token, "reversal")) == 204

    card = await _card(realdb, card_id)
    assert card.status == "completed" and card.amount_charged == Decimal("100.00")
    payment, state = await _leg(realdb, pay_id)
    assert payment.settled_amount == Decimal("100.00") and state == "covered"
    assert await _audits(realdb, card_id, "card.authorization_lapsed") == []
    (row,) = await _audits(realdb, card_id, "card.reversal_after_settlement")
    assert row.details["status"] == "completed" and row.details["lapse"] == "reversal"
    async with realdb.sessionmaker(TENANT)() as s:
        rebates = (
            (await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id)))
            .scalars()
            .all()
        )
    assert len(rebates) == 1


async def test_a_partial_reversal_leaves_the_card_authorized(realdb):
    """Part of the hold stands, so the card is still authorized."""
    card_id, _pay_id, token = await _leg_with_card(realdb, "nium")
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="100.00"))
            == 204
        )
        assert await _post(realdb, "nium", _body("nium", token, "reversal", amount="30.00")) == 204

    card = await _card(realdb, card_id)
    assert card.status == "charged" and card.amount_charged == Decimal("100.00")
    assert await _audits(realdb, card_id, "card.authorization_lapsed") == []


async def test_a_clearing_after_the_hold_lapsed_still_settles(realdb):
    """A force post: the clearing arrives after the authorization expired.
    Money moved, so the card completes and the leg is recorded and synced."""
    card_id, pay_id, token = await _leg_with_card(realdb, "nium")
    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="100.00"))
            == 204
        )
        assert await _post(realdb, "nium", _body("nium", token, "expiry")) == 204
        assert (
            await _post(realdb, "nium", _body("nium", token, "settlement", amount="100.00")) == 204
        )

    card = await _card(realdb, card_id)
    assert card.status == "completed" and card.amount_charged == Decimal("100.00")
    payment, state = await _leg(realdb, pay_id)
    assert payment.settled_amount == Decimal("100.00") and state == "covered"
    dispatched.assert_awaited_once()
    (row,) = await _audits(realdb, card_id, "card.settled")
    assert row.details["from"] == "active"


async def test_a_run_refuses_a_card_whose_authorization_lapsed(realdb):
    """The reissue side (decisions §549): a run re-executing the payable reuses
    the live card unless it is spent. Charged it is refused
    (`card_already_charged`); after the hold lapses it reads unspent, but a
    single-use card was closed at the provider by that authorization, so it is
    refused as `card_authorized_before` — from the durable
    `first_authorized_at`, which the lapse does not clear."""
    from app.services.card_issuance import card_settlement_block

    card_id, _pay_id, token = await _leg_with_card(realdb, "nium")
    assert (await _card(realdb, card_id)).first_authorized_at is None
    assert card_settlement_block(await _card(realdb, card_id), Decimal("100.00")) is None
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="100.00"))
            == 204
        )
        first = (await _card(realdb, card_id)).first_authorized_at
        assert first is not None
        assert card_settlement_block(await _card(realdb, card_id), Decimal("100.00")) == (
            "card_already_charged"
        )
        assert await _post(realdb, "nium", _body("nium", token, "expiry")) == 204
        # A second authorization keeps the FIRST moment.
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="100.00"))
            == 204
        )
        assert await _post(realdb, "nium", _body("nium", token, "reversal")) == 204

    card = await _card(realdb, card_id)
    assert card.status == "active" and card.charged_at is None
    assert card.first_authorized_at == first
    assert card_settlement_block(card, Decimal("100.00")) == "card_authorized_before"


async def _cancel(realdb, card_id) -> None:
    """The card as a payment void leaves it: cancelled at the provider and here
    (`test_payment_void_card_retry` drives the void itself)."""
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            VirtualCard.__table__.update()
            .where(VirtualCard.id == card_id)
            .values(status="cancelled")
        )
        await s.commit()


async def _reconciliation_flags(realdb, card_id):
    from app.models.exception import Exception as APException

    async with realdb.sessionmaker(TENANT)() as s:
        card = await s.get(VirtualCard, card_id)
        return list(
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == card.invoice_id,
                        APException.exception_type == "payment_reconciliation",
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.parametrize("provider", ["lithic", "nium", "mock"])
async def test_a_clearing_on_a_cancelled_card_is_flagged_never_dropped(realdb, provider):
    """Decisions §549: the hold lapsed, AP voided (card cancelled, invoice back
    in the pool), another rail may have paid it, and the merchant's force post
    on the old card clears. Before, no branch matched and the event vanished.
    Now: the card stays cancelled with no spend, nothing is written onto the
    payment and no sync runs, a `card.settled_after_cancel` row records the
    amount (string Decimal, last four only) and ONE payment-blocking
    `payment_reconciliation` flag stops the next run, however often it clears."""
    from app.services.payment_gates import PAYMENT_BLOCKING_EXCEPTION_TYPES

    card_id, pay_id, token = await _leg_with_card(realdb, provider)
    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        assert (
            await _post(realdb, provider, _body(provider, token, "authorization", amount="100.00"))
            == 204
        )
        assert await _post(realdb, provider, _body(provider, token, "expiry")) == 204
        await _cancel(realdb, card_id)
        assert (
            await _post(realdb, provider, _body(provider, token, "settlement", amount="100.00"))
            == 204
        )
        # A second clearing (another delivery id) does not pile up flags.
        assert (
            await _post(realdb, provider, _body(provider, token, "settlement", amount="100.00"))
            == 204
        )
    dispatched.assert_not_awaited()

    card = await _card(realdb, card_id)
    assert card.status == "cancelled" and card.amount_charged is None
    payment, state = await _leg(realdb, pay_id)
    assert payment.settled_amount is None and state == "in_flight"
    async with realdb.sessionmaker(TENANT)() as s:
        rebates = (
            (await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id)))
            .scalars()
            .all()
        )
    assert rebates == []
    assert await _audits(realdb, card_id, "card.settled") == []

    rows = await _audits(realdb, card_id, "card.settled_after_cancel")
    assert len(rows) == 2
    details = rows[0].details
    assert details["settled_amount"] == "100.00"  # string Decimal, never float
    assert details["status"] == "cancelled" and details["last_four"] == card.last_four
    assert token not in json.dumps(details)

    (flag,) = await _reconciliation_flags(realdb, card_id)
    assert "payment_reconciliation" in PAYMENT_BLOCKING_EXCEPTION_TYPES
    assert flag.status == "open" and flag.raised_by_user_id is None
    assert flag.description_code == "exception.card_settled_after_cancel"
    assert flag.description_params == {"lastFour": card.last_four or "????"}
    assert flag.erp_report["kind"] == "card_settled_after_cancel"
    assert flag.erp_report["settled_amount"] == "100.00"
    assert token not in json.dumps(flag.erp_report)


@pytest.mark.parametrize(
    "event_type", ["dispute_transaction.created", "three_ds_authentication.created"]
)
async def test_a_signed_lithic_event_that_is_no_card_stage_changes_nothing(realdb, event_type):
    """Decisions §549: these carry a `card_token` and words the old substring
    match read as a settlement ("transaction") or an authorization ("auth").
    Signed and on an `active` card, they change nothing."""
    card_id, pay_id, token = await _leg_with_card(realdb, "lithic")
    body = json.dumps(
        {
            "type": event_type,
            "event_id": str(uuid.uuid4()),
            "token": f"obj_{token}",
            "card_token": token,
            "status": "SETTLED",
            "amount": 10000,
        }
    ).encode("utf-8")
    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        assert await _post(realdb, "lithic", body) == 204
    dispatched.assert_not_awaited()

    card = await _card(realdb, card_id)
    assert card.status == "active" and card.amount_charged is None
    assert card.first_authorized_at is None
    payment, state = await _leg(realdb, pay_id)
    assert payment.settled_amount is None and state == "in_flight"
    for action in ("card.charged", "card.settled", "card.authorization_lapsed"):
        assert await _audits(realdb, card_id, action) == []


# --- A lapse delivered before its authorization (decisions §554) ----------


@pytest.mark.parametrize("provider", ["lithic", "nium", "mock"])
@pytest.mark.parametrize("lapse", ["reversal", "expiry"])
async def test_a_lapse_before_its_authorization_is_applied_when_it_arrives(realdb, provider, lapse):
    """Out of order: the reversal / expiry first (nothing to release, nothing
    changes), then its authorization. The authorization is applied as already
    lapsed — the card stays unspent with no figure — and the one
    `card.authorization_lapsed` row says `out_of_order: true`. The provider did
    authorize, so `first_authorized_at` is stamped and a re-run refuses the
    single-use card (§549); a void still reads it unspent."""
    from app.services.card_issuance import CARD_SPENT_STATUSES, card_settlement_block

    card_id, pay_id, token = await _leg_with_card(realdb, provider)
    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        assert await _post(realdb, provider, _body(provider, token, lapse)) == 204
        early = await _card(realdb, card_id)
        assert early.status == "active" and early.first_authorized_at is None
        assert await _audits(realdb, card_id, "card.authorization_lapsed") == []
        assert (
            await _post(realdb, provider, _body(provider, token, "authorization", amount="100.00"))
            == 204
        )
    dispatched.assert_not_awaited()

    card = await _card(realdb, card_id)
    assert card.status == "active" and card.status not in CARD_SPENT_STATUSES
    assert card.amount_charged is None and card.charged_at is None
    assert card.first_authorized_at is not None
    assert card_settlement_block(card, Decimal("100.00")) == "card_authorized_before"
    payment, state = await _leg(realdb, pay_id)
    assert payment.settled_amount is None and state == "in_flight"
    assert await _audits(realdb, card_id, "card.charged") == []
    (row,) = await _audits(realdb, card_id, "card.authorization_lapsed")
    assert row.details["out_of_order"] is True
    assert row.details["from"] == "active" and row.details["to"] == "active"
    assert row.details["lapse"] == lapse
    assert row.details["released_amount"] == "100.00"  # string Decimal
    assert token not in json.dumps(row.details)


async def test_a_lapse_with_no_authorization_never_marks_the_card_used(realdb):
    """A lapse alone is not an authorization: the card is not stamped as
    used, so a re-run may still converge onto it."""
    from app.services.card_issuance import card_settlement_block

    card_id, _pay_id, token = await _leg_with_card(realdb, "lithic")
    assert await _post(realdb, "lithic", _body("lithic", token, "expiry")) == 204
    card = await _card(realdb, card_id)
    assert card.status == "active" and card.first_authorized_at is None
    assert card_settlement_block(card, Decimal("100.00")) is None


async def test_an_early_lapse_of_another_transaction_does_not_lapse_this_one(realdb):
    """The memory is per provider transaction: a reversal of transaction A
    leaves the authorization of transaction B standing."""
    card_id, _pay_id, token = await _leg_with_card(realdb, "nium")
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert await _post(realdb, "nium", _body("nium", token, "reversal", txn="A1")) == 204
        assert (
            await _post(
                realdb,
                "nium",
                _body("nium", token, "authorization", amount="100.00", txn="B2"),
            )
            == 204
        )
    card = await _card(realdb, card_id)
    assert card.status == "charged" and card.amount_charged == Decimal("100.00")
    assert await _audits(realdb, card_id, "card.authorization_lapsed") == []


async def test_an_early_partial_reversal_leaves_the_late_authorization_standing(realdb):
    """A reversal naming less than the authorization leaves part of the hold,
    in order (§544) and out of order alike: the card is charged."""
    card_id, _pay_id, token = await _leg_with_card(realdb, "nium")
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert await _post(realdb, "nium", _body("nium", token, "reversal", amount="30.00")) == 204
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="100.00"))
            == 204
        )
    card = await _card(realdb, card_id)
    assert card.status == "charged" and card.amount_charged == Decimal("100.00")
    assert await _audits(realdb, card_id, "card.authorization_lapsed") == []


async def test_a_replayed_early_lapse_is_remembered_once(realdb):
    """The lapse row is unique per transaction: a second delivery of it (a new
    delivery id, so past the Redis dedupe) adds nothing, and the late
    authorization is lapsed once."""
    from app.models.virtual_card import CardTransactionEvent

    card_id, _pay_id, token = await _leg_with_card(realdb, "mock")
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert await _post(realdb, "mock", _body("mock", token, "reversal")) == 204
        assert await _post(realdb, "mock", _body("mock", token, "reversal")) == 204
        assert (
            await _post(realdb, "mock", _body("mock", token, "authorization", amount="100.00"))
            == 204
        )
    async with realdb.sessionmaker(TENANT)() as s:
        rows = (
            (
                await s.execute(
                    select(CardTransactionEvent).where(
                        CardTransactionEvent.virtual_card_id == card_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [(r.kind, r.lapse) for r in rows] == [("lapse", "reversal")]
    assert len(await _audits(realdb, card_id, "card.authorization_lapsed")) == 1
