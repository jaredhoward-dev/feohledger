"""A refused settlement holds the card as possibly spent (decisions §562).

§557 refuses a clearing that states no usable figure (missing, zero, negative,
another currency) and assumed the card stays `charged`. It need not: a lapse
returns a card to `active` (§544) and a force post can clear it afterwards. The
worked example: Nium approves 1000 USD, a reversal advice returns the card to
`active`, the settlement arrives with `authCurrency` EUR and is refused, and
the card still reads unspent — so someone cancels the "lapsed" card, voids the
leg, and the next run pays the invoice by ACH while the card charge stands.

Now the refusal records a durable `settlement_refused` fact on the card
(`card_transaction_events`), which a hand cancel (409), the void's card leg
(treated as charged) and a run's reuse (`card_settlement_block`) all read, and
opens one payment-blocking `card_settlement_refused` reconciliation flag so the
invoice enters no run until a person has reconciled the card.

Driven through the real signed webhook. Runs against the opt-in ``realdb``
fixture.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.payment import Payment
from app.models.virtual_card import CardTransactionEvent, VirtualCard
from tests.test_card_authorization_lapse import (
    TENANT,
    _audits,
    _body,
    _card,
    _leg_with_card,
    _post,
    _reconciliation_flags,
)
from tests.test_card_money_path_regression import NIUM_AMOUNT_FIELDS, _leg

pytestmark = pytest.mark.asyncio


def _nium_settlement_in(token: str, currency: str, amount: str) -> bytes:
    """Nium's settlement, its figure stated in ``currency`` (not the card's)."""
    body = json.loads(_body("nium", token, "settlement", amount=amount))
    _field, currency_field = NIUM_AMOUNT_FIELDS["CARD_SETTLEMENT_DEBIT_WEBHOOK"]
    body[currency_field] = currency
    return json.dumps(body).encode("utf-8")


async def _refused_facts(realdb, card_id) -> list[CardTransactionEvent]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (
                await s.execute(
                    select(CardTransactionEvent).where(
                        CardTransactionEvent.virtual_card_id == card_id,
                        CardTransactionEvent.kind == "settlement_refused",
                    )
                )
            )
            .scalars()
            .all()
        )


async def _cancel(realdb, card_id):
    async with realdb.client(key=TENANT, role="admin") as c:
        return await c.post(f"/api/cards/{card_id}/cancel")


async def _lapsed_then_refused(realdb, provider: str):
    """Authorize, lapse (the card back to `active`), then a settlement that
    states no usable figure. Returns (card_id, pay_id, token)."""
    card_id, pay_id, token = await _leg_with_card(realdb, provider, amount=Decimal("1000.00"))
    if provider == "nium":
        refused = _nium_settlement_in(token, "EUR", "1000.00")
    else:
        refused = _body(provider, token, "settlement")  # no figure at all
    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        assert (
            await _post(realdb, provider, _body(provider, token, "authorization", amount="1000.00"))
            == 204
        )
        assert await _post(realdb, provider, _body(provider, token, "reversal")) == 204
        assert (await _card(realdb, card_id)).status == "active"
        assert await _post(realdb, provider, refused) == 204
    dispatched.assert_not_awaited()
    return card_id, pay_id, token


@pytest.mark.parametrize("provider", ["nium", "lithic"])
async def test_a_refused_settlement_on_a_lapsed_card_holds_it_as_spent(realdb, provider):
    """The worked example. The card reads `active`, but: the refusal is
    recorded durably, one blocking flag is open, the run gate excludes the
    invoice, a hand cancel is a coded 409 that leaves the card live, and the
    void's card leg reports it charged rather than closing it."""
    from app.services.payment_gates import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services.payment_runs import blocked_invoice_ids

    card_id, pay_id, token = await _lapsed_then_refused(realdb, provider)

    card = await _card(realdb, card_id)
    assert card.status == "active" and card.amount_charged is None
    payment, state = await _leg(realdb, pay_id)
    assert payment.settled_amount is None and state == "in_flight"
    (fact,) = await _refused_facts(realdb, card_id)
    assert fact.amount is None and token not in (fact.event_key or "")

    (flag,) = await _reconciliation_flags(realdb, card_id)
    assert "payment_reconciliation" in PAYMENT_BLOCKING_EXCEPTION_TYPES
    assert flag.status == "open" and flag.raised_by_user_id is None
    assert flag.description_code == "exception.card_settlement_refused"
    assert flag.description_params == {"lastFour": card.last_four or "????"}
    assert flag.erp_report["kind"] == "card_settlement_refused"
    assert flag.erp_report["status"] == "active"
    assert token not in json.dumps(flag.erp_report)
    async with realdb.sessionmaker(TENANT)() as s:
        assert await blocked_invoice_ids(s, [card.invoice_id]) == {card.invoice_id}

    (row,) = await _audits(realdb, card_id, "card.settlement_refused")
    assert row.details["status"] == "active" and row.details["reason"] == "no_amount"

    resp = await _cancel(realdb, card_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "card_cancel_settlement_refused"
    assert (await _card(realdb, card_id)).status == "active"
    assert await _audits(realdb, card_id, "card.cancelled") == []

    from app.api.payments import _cancel_card_for_void

    org = SimpleNamespace(id=realdb.info(TENANT).org_id, settings={})
    async with realdb.sessionmaker(TENANT)() as s:
        leg = await s.get(Payment, pay_id)
        with patch("app.services.card_issuance.cancel_card_at_provider", new=AsyncMock()) as call:
            outcome = await _cancel_card_for_void(
                s, payment=leg, org=org, user=SimpleNamespace(id=uuid.uuid4())
            )
        await s.rollback()
    assert outcome == "card_already_charged"
    call.assert_not_awaited()


async def test_a_run_never_reuses_a_card_a_settlement_was_refused_on(realdb):
    """`card_settlement_block` refuses the card before its other checks, so a
    run converging onto it fails the leg instead of asserting money moved."""
    from app.services.card_issuance import card_settlement_block

    card_id, _pay_id, _token = await _lapsed_then_refused(realdb, "nium")
    card = await _card(realdb, card_id)
    assert (
        card_settlement_block(card, Decimal("1000.00"), settlement_refused=True)
        == "card_settlement_refused"
    )
    # Without the fact it would have been the lapse's own refusal (§549).
    assert card_settlement_block(card, Decimal("1000.00")) == "card_authorized_before"


async def test_a_refused_settlement_on_an_authorized_card_is_flagged_and_outlives_a_lapse(
    realdb,
):
    """On a `charged` card the leg stays in flight, so no run could pay the
    invoice today; the flag opens anyway (blocking: it is the only thing that
    reaches a person), and the durable fact still refuses a cancel once a later
    reversal returns the card to `active`."""
    card_id, _pay_id, token = await _leg_with_card(realdb, "nium", amount=Decimal("1000.00"))
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert (
            await _post(realdb, "nium", _body("nium", token, "authorization", amount="1000.00"))
            == 204
        )
        assert await _post(realdb, "nium", _nium_settlement_in(token, "EUR", "1000.00")) == 204
        card = await _card(realdb, card_id)
        assert card.status == "charged" and card.amount_charged == Decimal("1000.00")
        (flag,) = await _reconciliation_flags(realdb, card_id)
        assert flag.erp_report["kind"] == "card_settlement_refused"
        assert flag.erp_report["status"] == "charged"

        assert await _post(realdb, "nium", _body("nium", token, "reversal")) == 204
    assert (await _card(realdb, card_id)).status == "active"
    resp = await _cancel(realdb, card_id)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "card_cancel_settlement_refused"


async def test_repeated_refusals_record_each_delivery_but_open_one_flag(realdb):
    """A replay of one delivery records nothing new; a second refused delivery
    is a second fact, and the flag stays one (deduped by kind, §535)."""
    card_id, _pay_id, token = await _leg_with_card(realdb, "lithic")
    body = _body("lithic", token, "settlement")
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert await _post(realdb, "lithic", body, event_id="evt-1") == 204
        # The same delivery id again: the Redis claim (or, without it, the
        # per-card unique key) keeps it to one fact.
        assert await _post(realdb, "lithic", body, event_id="evt-1") == 204
        assert await _post(realdb, "lithic", body, event_id="evt-2") == 204
    assert sorted(f.event_key for f in await _refused_facts(realdb, card_id)) == [
        "delivery:evt-1",
        "delivery:evt-2",
    ]
    assert len(await _reconciliation_flags(realdb, card_id)) == 1


async def test_a_card_with_no_refused_settlement_still_cancels(realdb):
    """The gate reads the fact, not the status: an unspent card nobody's
    settlement was refused on cancels as before."""
    card_id, _pay_id, _token = await _leg_with_card(realdb, "mock")
    resp = await _cancel(realdb, card_id)
    assert resp.status_code == 200, resp.text
    card = await _card(realdb, card_id)
    assert isinstance(card, VirtualCard) and card.status == "cancelled"
