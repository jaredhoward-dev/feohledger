"""Card webhook event classification + amount normalization.

Two bugs the pure helpers in `app.api.cards` close:

1. A naive substring match (`"auth" in event_type`) treated a *declined* or
   *reversed* authorization as a real charge — flipping the card to `charged`
   on money that never moved and minting a rebate on it.

2. Lithic webhook amounts are MINOR units (cents); Nium are MAJOR units. The
   handler divided BOTH by 100, recording 1/100th of every Nium charge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.api.cards import _classify_card_event, _normalize_charge_amount

# ── Event classification ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "event_type",
    [
        "authorization",
        "authorization.created",
        "card.authorization",
        "auth.approved",
    ],
)
def test_genuine_authorizations_classify_as_charge(event_type):
    is_auth, is_settled = _classify_card_event(event_type)
    assert is_auth is True
    assert is_settled is False


@pytest.mark.parametrize(
    "event_type",
    [
        "authorization.decline",
        "authorization.declined",
        "authorization.reversal",
        "authorization.reversed",
        "transaction.voided",
        "transaction.refund",
        "transaction.return",
        "card.cancelled",
        "authorization.expired",
    ],
)
def test_declines_and_reversals_are_neither_charge_nor_settlement(event_type):
    # The core fix: a declined/reversed/voided event must NOT charge the card.
    is_auth, is_settled = _classify_card_event(event_type)
    assert is_auth is False, f"{event_type!r} must not be treated as a charge"
    assert is_settled is False, f"{event_type!r} must not be treated as a settlement"


@pytest.mark.parametrize(
    "event_type",
    ["transaction", "transaction.settled", "settlement.completed"],
)
def test_settlements_classify_as_settled(event_type):
    is_auth, is_settled = _classify_card_event(event_type)
    assert is_settled is True


def test_empty_event_type_is_inert():
    assert _classify_card_event("") == (False, False)
    assert _classify_card_event(None) == (False, False)


# ── Amount normalization (provider unit semantics) ────────────────────


def test_lithic_amount_is_minor_units_divided_by_100():
    # Lithic sends cents: 150000 == $1,500.00
    assert _normalize_charge_amount("lithic", 150000, Decimal("0")) == Decimal("1500.00")


def test_nium_amount_is_major_units_not_divided():
    # Nium sends major units: 50.00 == $50.00 (the bug recorded $0.50).
    assert _normalize_charge_amount("nium", "50.00", Decimal("0")) == Decimal("50.00")
    assert _normalize_charge_amount("nium", 50, Decimal("0")) == Decimal("50")


def test_falsy_amount_falls_back_to_card_limit():
    fallback = Decimal("100.00")
    assert _normalize_charge_amount("lithic", 0, fallback) == fallback
    assert _normalize_charge_amount("nium", None, fallback) == fallback


def test_unparseable_amount_falls_back():
    fallback = Decimal("100.00")
    assert _normalize_charge_amount("lithic", "not-a-number", fallback) == fallback


def test_normalized_amount_is_exact_decimal():
    # Money invariant: never float. A repeating value stays exact.
    result = _normalize_charge_amount("nium", "33.33", Decimal("0"))
    assert isinstance(result, Decimal)
    assert result == Decimal("33.33")


# ── Authorization lapse (decisions §544) ──────────────────────────────


@pytest.mark.parametrize(
    ("event_type", "lapse"),
    [
        ("authorization.reversal", "reversal"),
        ("authorization.reversed", "reversal"),
        ("transaction.voided", "reversal"),
        ("CARD_TRANSACTION_REVERSAL_WEBHOOK", "reversal"),  # Nium's template
        ("authorization.expired", "expiry"),
        ("AUTHORIZATION_EXPIRY", "expiry"),
        ("authorization.declined", "decline"),
        ("authorization.decline", "decline"),
    ],
)
def test_a_lapse_is_classified_by_kind(event_type, lapse):
    from app.api.cards import _classify_card_lapse

    assert _classify_card_lapse(event_type) == lapse
    # And it is never a charge or a settlement.
    assert _classify_card_event(event_type) == (False, False)


@pytest.mark.parametrize(
    "event_type",
    [
        # Post-settlement: a refund / chargeback flow, never an un-charge.
        "CARD_SETTLEMENT_REVERSAL_WEBHOOK",
        "settlement.reversal",
        "transaction.refund",
        "transaction.return",
        "chargeback.created",
        "card.cancelled",
        "authorization",
        "settlement",
        "",
        None,
    ],
)
def test_everything_else_is_not_a_lapse(event_type):
    from app.api.cards import _classify_card_lapse

    assert _classify_card_lapse(event_type) is None


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        ("PENDING", "authorization"),
        ("SETTLED", "settlement"),
        ("VOIDED", "authorization.reversal"),
        ("EXPIRED", "authorization.expired"),
        ("DECLINED", "authorization.declined"),
        ("pending", "authorization"),
        ("BOUNCED", ""),
        (None, ""),
    ],
)
def test_lithic_transaction_updates_are_read_by_status(status, event_type):
    """`card_transaction.updated` names no outcome; its `status` does. An
    unknown status is inert — never the event name, which reads as a
    settlement by substring."""
    from app.api.cards import _lithic_event_type

    body = {"type": "card_transaction.updated", "status": status}
    assert _lithic_event_type(body) == event_type
    assert _classify_card_event(_lithic_event_type({"type": "card_transaction.updated"})) == (
        False,
        False,
    )


def test_other_lithic_events_keep_their_name():
    from app.api.cards import _lithic_event_type

    assert _lithic_event_type({"type": "authorization"}) == "authorization"
    assert _lithic_event_type({}) == ""


def test_lithic_transaction_stages_dedupe_separately():
    """The transaction token is the same on every update of it; keyed on it
    alone, the reversal would be deduped as a replay of the authorization."""
    from app.api.cards import _lithic_event_id

    base = {"type": "card_transaction.updated", "token": "txn_1"}
    pending = _lithic_event_id({**base, "status": "PENDING"})
    voided = _lithic_event_id({**base, "status": "VOIDED"})
    assert pending != voided
    assert pending == _lithic_event_id({**base, "status": "PENDING"})
    assert _lithic_event_id({**base, "status": "VOIDED", "event_id": "evt_9"}) == "evt_9"
    assert _lithic_event_id({"type": "authorization", "token": "t"}) == "t"


def test_a_partial_reversal_is_one_below_the_authorized_figure():
    from types import SimpleNamespace

    from app.api.cards import _is_partial_reversal

    card = SimpleNamespace(amount_charged=Decimal("100.00"), currency="USD")
    assert _is_partial_reversal("nium", "30.00", card) is True
    assert _is_partial_reversal("nium", "100.00", card) is False
    assert _is_partial_reversal("nium", None, card) is False
    # Lithic's VOIDED is set only when nothing is held: always whole.
    assert _is_partial_reversal("lithic", 3000, card) is False
    assert _is_partial_reversal("nium", "30.00", SimpleNamespace(amount_charged=None)) is False
