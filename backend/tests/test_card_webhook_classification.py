"""Card webhook event classification + amount normalization.

Two bugs the pure helpers in `app.api.cards` close:

1. A naive substring match (`"auth" in event_type`) treated a *declined* or
   *reversed* authorization as a real charge — flipping the card to `charged`
   on money that never moved and minting a rebate on it. Since decisions §549
   every provider's names are read from an explicit map; anything unlisted is
   no stage at all.

2. Lithic webhook amounts are MINOR units (cents); Nium are MAJOR units. The
   handler divided BOTH by 100, recording 1/100th of every Nium charge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# ── Event classification: explicit per-provider maps (decisions §549) ──
from app.api.cards import (  # noqa: E402
    _card_event_stage,
    _classify_card_event,
    _classify_card_lapse,
    _lithic_event_type,
    _normalize_charge_amount,
)

_LITHIC_STAGES = {
    "PENDING": "authorization",
    "SETTLED": "settlement",
    "VOIDED": "authorization.reversal",
    "EXPIRED": "authorization.expired",
    "DECLINED": "authorization.declined",
}


def _lithic_stage(body: dict) -> str:
    return _card_event_stage("lithic", _lithic_event_type(body))


@pytest.mark.parametrize(("status", "stage"), list(_LITHIC_STAGES.items()))
def test_each_lithic_transaction_status_maps_to_its_stage(status, stage):
    assert _lithic_stage({"event_type": "card_transaction.updated", "status": status}) == stage


@pytest.mark.parametrize(
    "event_type",
    [
        # Every one carries words the old substring match read as a stage.
        "dispute_transaction.created",
        "dispute_transaction.updated",
        "card_transaction.enhanced_data.created",
        "card_transaction.enhanced_data.updated",
        "three_ds_authentication.created",
        "three_ds_authentication.updated",
        "card_authorization.challenge",
        "settlement_report.updated",
        "book_transfer_transaction.created",
        "payment_transaction.updated",
        # Our own names are not Lithic's: only `card_transaction.updated` is.
        "authorization",
        "transaction.settled",
        "",
    ],
)
def test_other_lithic_event_types_are_no_stage(event_type):
    # Even with a status a card transaction would carry.
    assert _lithic_stage({"event_type": event_type, "status": "SETTLED"}) == ""


def test_lithic_names_the_event_only_in_event_type():
    """The webhook body is the Transaction plus `event_type`
    (lithic-openapi.yml); a `type` key is not Lithic's and reads as nothing
    (decisions §555)."""
    assert _lithic_stage({"type": "card_transaction.updated", "status": "SETTLED"}) == ""
    assert _classify_card_event("") == (False, False)


@pytest.mark.parametrize(
    ("name", "stage"),
    [
        ("CARD_POS_APPROVED_WEBHOOK", "authorization"),
        ("CARD_SETTLEMENT_DEBIT_WEBHOOK", "settlement"),
        ("CARD_TRANSACTION_REVERSAL_WEBHOOK", "authorization.reversal"),
        ("CARD_TRANSACTION_REVERSAL_ADVICE_WEBHOOK", "authorization.reversal"),
        ("CARD_INSUFFICIENT_FUNDS_DECLINED_WEBHOOK", "authorization.declined"),
        ("CARD_WRONG_EXPIRY_WEBHOOK", "authorization.declined"),
        ("CARD_WRONG_CVV2_WEBHOOK", "authorization.declined"),
        ("CARD_MISC_WEBHOOK", "authorization.declined"),
        ("CARD_EXPIRED_CARD_WEBHOOK", "authorization.declined"),
        ("CARD_SYSTEM_DOWN_WEBHOOK", "authorization.declined"),
    ],
)
def test_each_known_nium_name_maps_to_its_stage(name, stage):
    assert _card_event_stage("nium", name) == stage


@pytest.mark.parametrize(
    "name",
    [
        # Post-settlement money coming back: a refund flow, never a stage.
        "CARD_SETTLEMENT_CREDIT_WEBHOOK",
        "CARD_SETTLEMENT_REVERSAL_WEBHOOK",
        "CARD_REFUND_APPROVAL_WEBHOOK",
        "CARD_3DS_OTP_WEBHOOK",
        "CARD_ADD_CARD_WEBHOOK",
        # The card's own expiry alert, not an authorization expiring.
        "CARD_EXPIRY_CARD_WEBHOOK",
        "CARD_ATM_APPROVED_WEBHOOK",
        # Never in Nium's docs (decisions §555): §549 kept them, now gone.
        "authorization.approved",
        "transaction.settled",
        "authorization.reversal",
        "authorization.expired",
        "authorization.declined",
        "transaction",
        "settlement.completed",
        "transaction.refund",
        "chargeback.created",
        "",
        None,
    ],
)
def test_unlisted_nium_names_are_no_stage(name):
    assert _card_event_stage("nium", name) == ""


def test_each_mock_simulator_event_maps_to_its_stage():
    from app.services.card_adapters.mock_adapter import (
        MOCK_EVENT_AUTHORIZATION,
        MOCK_EVENT_DECLINE,
        MOCK_EVENT_EXPIRY,
        MOCK_EVENT_REVERSAL,
        MOCK_EVENT_SETTLEMENT,
    )

    assert _card_event_stage("mock", MOCK_EVENT_AUTHORIZATION) == "authorization"
    assert _card_event_stage("mock", MOCK_EVENT_SETTLEMENT) == "settlement"
    assert _card_event_stage("mock", MOCK_EVENT_REVERSAL) == "authorization.reversal"
    assert _card_event_stage("mock", MOCK_EVENT_EXPIRY) == "authorization.expired"
    assert _card_event_stage("mock", MOCK_EVENT_DECLINE) == "authorization.declined"
    assert _card_event_stage("mock", "transaction.settled") == ""


def test_an_unknown_provider_has_no_stages():
    assert _card_event_stage("stripe", "authorization") == ""


def test_stages_classify_exactly():
    assert _classify_card_event("authorization") == (True, False)
    assert _classify_card_event("settlement") == (False, True)
    for stage in ("authorization.reversal", "authorization.expired", "authorization.declined"):
        assert _classify_card_event(stage) == (False, False)
    # A raw provider name is not a stage: no substring reading survives.
    for raw in ("card_transaction.updated", "transaction.settled", "auth.approved", ""):
        assert _classify_card_event(raw) == (False, False)


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
    ("stage", "lapse"),
    [
        ("authorization.reversal", "reversal"),
        ("authorization.expired", "expiry"),
        ("authorization.declined", "decline"),
    ],
)
def test_a_lapse_is_classified_by_kind(stage, lapse):
    assert _classify_card_lapse(stage) == lapse
    # And it is never a charge or a settlement.
    assert _classify_card_event(stage) == (False, False)


@pytest.mark.parametrize(
    "stage",
    ["authorization", "settlement", "", "transaction.voided", "CARD_TRANSACTION_REVERSAL_WEBHOOK"],
)
def test_everything_else_is_not_a_lapse(stage):
    # A raw provider name reaches the lapse reader only through its map.
    assert _classify_card_lapse(stage) is None


def test_lithic_unknown_status_is_inert():
    evt = "card_transaction.updated"
    assert _lithic_event_type({"event_type": evt, "status": "BOUNCED"}) == ""
    assert _lithic_event_type({"event_type": evt}) == ""
    assert _lithic_event_type({"event_type": evt, "status": "pending"}) == "authorization"
    assert _lithic_event_type({}) == ""


def test_lithic_and_nium_dedupe_on_their_delivery_id_header():
    """Each provider's own delivery id, never a body field: Lithic's signed
    `webhook-id`, Nium's `x-request-id` (decisions §555)."""
    from app.api.cards import _CARD_EVENT_ID_HEADERS

    assert _CARD_EVENT_ID_HEADERS == {"lithic": "webhook-id", "nium": "x-request-id"}


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
