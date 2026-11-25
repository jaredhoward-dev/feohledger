"""A run's virtual-card leg: `record_card_spend` and `payment_coverage`
(`services/payment_settlement_record`, decisions §537, §544).

Golden cases on real Postgres (both functions read rows):

* `record_card_spend` writes a SETTLED card's charge onto its run payment —
  the figure and the card's currency, through `persistable_settled_amount` —
  and writes nothing for a card that is only authorized, a card no payment
  owns, a non-card or non-completed payment, or a card with no figure. A re-run
  writes the same figure. The column's largest value is stored; one past it is
  flagged unstorable, never truncated.
* `payment_coverage` of a card leg per card status: anything short of settled
  (`created` / `sent` / `active` / authorized `charged` / `cancelled`, or no
  card) is `in_flight`, whatever the payment carries; settled is judged on the
  figure (covered, short, a foreign currency uncertain), falling back to the
  card's own figure when none was stamped. A non-card payment is unchanged.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.virtual_card import VirtualCard
from app.services.payment_settlement import (
    COVERAGE_COVERED,
    COVERAGE_IN_FLIGHT,
    COVERAGE_SHORT,
    COVERAGE_UNCERTAIN,
)
from app.services.payment_settlement_record import payment_coverage, record_card_spend

TENANT = "a"
LIMIT = Decimal("500.00")


async def _leg(
    realdb,
    *,
    card_status: str | None = "active",
    charged: Decimal | None = None,
    card_currency: str = "USD",
    method: str = "virtual_card",
    payment_status: str = "completed",
    linked: bool = True,
) -> SimpleNamespace:
    """An invoice, its run payment and (unless ``card_status`` is None) its card."""
    org_id = realdb.info(TENANT).org_id
    inv_id, corr, run_id, pay_id, card_id = (uuid.uuid4() for _ in range(5))
    async with realdb.sessionmaker(TENANT)() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        s.add(
            Invoice(
                id=inv_id,
                entity_id=ent,
                invoice_number=f"PSR-{uuid.uuid4().hex[:8]}",
                vendor_name="Card Vendor",
                amount=LIMIT,
                currency="USD",
                status=InvoiceStatus.payment_scheduled,
                organization_id=org_id,
                correlation_id=corr,
                uploaded_by_id=None,  # seeded directly; no employee created it
            )
        )
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                status="completed",
                total_amount=LIMIT,
                requires_cfo_approval=False,
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=LIMIT,
                method=method,
                status=payment_status,
                correlation_id=corr,
            )
        )
        await s.flush()
        if card_status is not None:
            s.add(
                VirtualCard(
                    id=card_id,
                    invoice_id=inv_id,
                    payment_id=pay_id if linked else None,
                    organization_id=org_id,
                    entity_id=ent,
                    correlation_id=corr,
                    card_provider="mock",
                    provider_card_id=f"card-{card_id.hex[:10]}",
                    last_four="9999",
                    amount_limit=LIMIT,
                    amount_charged=charged,
                    currency=card_currency,
                    status=card_status,
                )
            )
        await s.commit()
    return SimpleNamespace(invoice_id=inv_id, payment_id=pay_id, card_id=card_id)


async def _record(realdb, leg) -> tuple[Payment | None, Payment]:
    """Run ``record_card_spend`` on the leg's card and commit; return what it
    returned and the payment as read back afterwards."""
    async with realdb.sessionmaker(TENANT)() as s:
        card = await s.get(VirtualCard, leg.card_id)
        returned = await record_card_spend(s, card)
        await s.commit()
    async with realdb.sessionmaker(TENANT)() as s:
        return returned, await s.get(Payment, leg.payment_id)


async def _coverage(realdb, leg):
    async with realdb.sessionmaker(TENANT)() as s:
        payment = await s.get(Payment, leg.payment_id)
        return await payment_coverage(s, payment, "USD")


# ---------------------------------------------------------------------------
# record_card_spend
# ---------------------------------------------------------------------------


async def test_a_settled_charge_is_written_onto_its_payment(realdb):
    leg = await _leg(realdb, card_status="completed", charged=Decimal("480.25"))

    returned, payment = await _record(realdb, leg)

    assert returned is not None and returned.id == leg.payment_id
    assert payment.settled_amount == Decimal("480.25")
    assert isinstance(payment.settled_amount, Decimal)
    assert payment.settled_currency == "USD"
    assert payment.settled_amount_unstorable is False


async def test_an_authorized_charge_writes_nothing(realdb):
    """`charged` is the authorization: a hold, not money moved (§544)."""
    leg = await _leg(realdb, card_status="charged", charged=LIMIT)

    returned, payment = await _record(realdb, leg)

    assert returned is None
    assert (payment.settled_amount, payment.settled_currency) == (None, None)


async def test_the_card_currency_is_recorded_even_when_it_is_not_the_invoices(realdb):
    """The figure is recorded as the card reported it; coverage, not the
    writer, decides what a foreign currency means (uncertain, below)."""
    leg = await _leg(realdb, card_status="completed", charged=LIMIT, card_currency="EUR")

    _, payment = await _record(realdb, leg)

    assert (payment.settled_amount, payment.settled_currency) == (LIMIT, "EUR")
    assert (await _coverage(realdb, leg)).state == COVERAGE_UNCERTAIN


async def test_a_card_no_run_payment_owns_writes_nothing(realdb):
    leg = await _leg(realdb, card_status="completed", charged=LIMIT, linked=False)

    returned, payment = await _record(realdb, leg)

    assert returned is None
    assert payment.settled_amount is None


@pytest.mark.parametrize(
    ("method", "payment_status"),
    [("ach", "completed"), ("virtual_card", "pending"), ("virtual_card", "voided")],
)
async def test_a_non_card_or_non_completed_payment_is_not_written(realdb, method, payment_status):
    leg = await _leg(
        realdb,
        card_status="completed",
        charged=LIMIT,
        method=method,
        payment_status=payment_status,
    )

    returned, payment = await _record(realdb, leg)

    assert returned is None
    assert payment.settled_amount is None


async def test_a_settled_card_with_no_figure_writes_nothing(realdb):
    leg = await _leg(realdb, card_status="completed", charged=None)

    returned, payment = await _record(realdb, leg)

    assert returned is None
    assert payment.settled_amount is None


async def test_a_re_run_writes_the_same_figure(realdb):
    leg = await _leg(realdb, card_status="completed", charged=Decimal("499.99"))

    await _record(realdb, leg)
    _, payment = await _record(realdb, leg)

    assert (payment.settled_amount, payment.settled_currency) == (Decimal("499.99"), "USD")
    assert payment.settled_amount_unstorable is False


async def test_the_columns_largest_figure_is_stored_exactly(realdb):
    """`payments.settled_amount` is NUMERIC(15, 2): 13 integer digits, 2 decimal."""
    biggest = Decimal("9999999999999.99")
    leg = await _leg(realdb, card_status="completed", charged=biggest)

    _, payment = await _record(realdb, leg)

    assert payment.settled_amount == biggest
    assert payment.settled_amount_unstorable is False


async def test_a_figure_past_the_column_is_flagged_not_truncated(realdb):
    """One past the column's largest value — only reachable in memory, since
    the card's own column is as narrow — is recorded as unstorable (coverage
    then holds it as uncertain), never cut to fit or read as nothing reported."""
    leg = await _leg(realdb, card_status="completed", charged=LIMIT)
    async with realdb.sessionmaker(TENANT)() as s:
        card = SimpleNamespace(
            payment_id=leg.payment_id,
            status="completed",
            amount_charged=Decimal("10000000000000.00"),
            currency="USD",
        )
        assert await record_card_spend(s, card) is not None
        await s.commit()
    async with realdb.sessionmaker(TENANT)() as s:
        payment = await s.get(Payment, leg.payment_id)
    assert payment.settled_amount is None
    assert payment.settled_amount_unstorable is True
    assert (await _coverage(realdb, leg)).state == COVERAGE_UNCERTAIN


# ---------------------------------------------------------------------------
# payment_coverage per card status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["created", "sent", "active", "charged", "cancelled"])
async def test_a_card_leg_short_of_settled_is_in_flight(realdb, status):
    leg = await _leg(realdb, card_status=status, charged=LIMIT if status == "charged" else None)

    c = await _coverage(realdb, leg)

    assert c.state == COVERAGE_IN_FLIGHT
    assert c.completes_invoice is False


async def test_a_card_leg_with_no_card_is_in_flight(realdb):
    leg = await _leg(realdb, card_status=None)
    assert (await _coverage(realdb, leg)).state == COVERAGE_IN_FLIGHT


async def test_an_authorized_leg_ignores_a_figure_already_on_the_payment(realdb):
    """A figure stamped at the authorization (the §537 build did) is not
    evidence of money moved; only the card's settlement is (§544)."""
    leg = await _leg(realdb, card_status="charged", charged=LIMIT)
    async with realdb.sessionmaker(TENANT)() as s:
        payment = await s.get(Payment, leg.payment_id)
        payment.settled_amount, payment.settled_currency = LIMIT, "USD"
        await s.commit()

    assert (await _coverage(realdb, leg)).state == COVERAGE_IN_FLIGHT


@pytest.mark.parametrize(
    ("charged", "state"),
    [(LIMIT, COVERAGE_COVERED), (Decimal("650.00"), COVERAGE_COVERED), ("300.00", COVERAGE_SHORT)],
)
async def test_a_settled_leg_is_judged_on_its_figure(realdb, charged, state):
    leg = await _leg(realdb, card_status="completed", charged=Decimal(charged))
    await _record(realdb, leg)

    c = await _coverage(realdb, leg)

    assert c.state == state
    if state == COVERAGE_SHORT:
        assert c.shortfall == Decimal("200.00")


async def test_a_settled_leg_with_nothing_stamped_reads_the_cards_figure(realdb):
    """A leg settled before its figure was written (no backfill): the card's
    own `amount_charged` is the figure."""
    leg = await _leg(realdb, card_status="completed", charged=Decimal("300.00"))

    c = await _coverage(realdb, leg)

    assert (c.state, c.shortfall) == (COVERAGE_SHORT, Decimal("200.00"))


async def test_a_non_card_payment_with_no_figure_still_fails_open(realdb):
    leg = await _leg(realdb, card_status=None, method="ach")
    assert (await _coverage(realdb, leg)).state == COVERAGE_COVERED
