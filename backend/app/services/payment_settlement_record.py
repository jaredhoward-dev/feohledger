"""Recording a settlement against a payment — the DB-touching half.

`services/payment_settlement.py` is pure: it decides a verdict and says what
`payments.settled_amount` can hold. This module is what WRITES that verdict —
fetching a missing figure from the processor, persisting the settled columns,
and raising the payment-blocking exception a discrepancy warrants.

It exists because there are **two** paths on which a payment reaches
`completed`, and they had drifted. `payment_webhook` verified the settlement,
put the verdict on the append-only audit row and opened a `fraud_flag` on a
mismatch. The reconciler backstop — the path that runs precisely when the
webhook never arrived, i.e. the case with the LEAST evidence — persisted the
figure and stopped there: no verdict, no audit block, no exception. A rail
reporting a 10x overpayment settled silently and `settlement_coverage` marked
the invoice `paid`, because over-settlement is `covered` by design. A short
settlement stranded the invoice at `payment_scheduled` with nothing in the
queue to explain it. And because the webhook handler refuses a payment that is
already terminal, a late webhook could never supply the missing verdict.

One implementation, both callers, so the two cannot disagree about what
"verified" means.

The same drift reappeared one field over. Realized FX gain/loss — the number a
foreign-currency invoice books at the moment it actually settles — was computed
inline in the webhook handler and nowhere else, so every cross-currency payment
the reconciler backstop recovered lost its realized-FX row silently. Rather
than copy the block, :func:`record_completion` now owns the WHOLE "a payment
just reached ``completed``" bookkeeping — settlement verdict AND realized FX —
and both paths call it. A third completion path cannot reintroduce the gap
without deliberately re-deriving what this module already returns;
``tests/test_payment_completion_record.py`` is the drift guard on that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.services.international_payments import realized_fx_gain_loss_for_settlement
from app.services.payment_settlement import (
    SettlementVerification,
    discrepancy_finding,
    persistable_settled_amount,
    verify_settlement,
)

logger = logging.getLogger(__name__)


async def record_settlement(
    db: AsyncSession,
    *,
    payment: Payment,
    adapter,
    invoice: Invoice | None,
    reported_amount: Decimal | None = None,
    reported_currency: str | None = None,
) -> SettlementVerification:
    """Verify what the rail says it settled and persist it onto ``payment``.

    ``reported_amount``/``reported_currency`` are what the caller already
    holds — the webhook event's own figures. When the amount is absent (a rail
    like Dwolla sends a bare ``{id, topic, resourceId}`` envelope, and
    ``get_payment_status`` returns a bare status by design) the processor is
    asked directly via the optional ``fetch_settlement`` capability.

    Best-effort on every axis: an adapter without the capability reports
    ``available=False``, and ANY failure leaves the verdict `unverified`
    rather than breaking the money path — a settlement fetch must never break
    the webhook that is recording money movement, nor halt the sweep's tick.

    Returns the verdict for the caller to put on its audit row. Does not
    commit.
    """
    if reported_amount is None and getattr(payment, "provider_payment_id", None):
        try:
            report = await adapter.fetch_settlement(payment.provider_payment_id)
        except Exception as exc:  # noqa: BLE001 - best-effort by contract
            # Class only, never the message — a processor SDK error string can
            # embed partial account data (PII-out-of-logs invariant).
            logger.warning(
                "settlement fetch failed for payment=%s: %s",
                payment.id,
                exc.__class__.__name__,
            )
        else:
            if report.available and report.amount is not None:
                reported_amount = report.amount
                reported_currency = report.currency

    verification = verify_settlement(
        reported_amount=reported_amount,
        reported_currency=reported_currency,
        target_amount=payment.amount,
        target_currency=(invoice.currency if invoice is not None else None),
        source_amount=payment.source_amount,
        source_currency=payment.source_currency,
    )

    # Persist what the rail says it moved, beside what AP authorized. A rail
    # that reported nothing leaves the columns NULL rather than writing a zero:
    # NULL means "no figure on record" and fails OPEN in the coverage check,
    # while a 0 would read as a total shortfall and hold every such invoice
    # forever. A figure too wide for NUMERIC(15, 2) is recorded as the
    # `settled_amount_unstorable` flag — assigning it would raise at the flush
    # and roll back the whole transaction, which on the webhook path means the
    # processor retries into the same failure forever and on the sweep path
    # aborts the entire tick.
    storable, unstorable = persistable_settled_amount(verification.settled_amount)
    if storable is not None:
        payment.settled_amount = storable
        payment.settled_currency = verification.settled_currency
    if unstorable:
        payment.settled_amount_unstorable = True
        payment.settled_currency = verification.settled_currency

    return verification


@dataclass(frozen=True)
class SettlementCompletion:
    """Everything one ``completed`` payment books, for whichever path got there.

    Two facts, and they are recorded together because they are decided
    together and ride the SAME append-only audit row:

    * ``verification`` — did the rail move what AP authorized?
    * ``realized_fx_gain_loss`` — for a foreign-currency invoice, the
      difference between the liability accrued at the invoice's locked rate
      and the cash that actually left in the home currency. ``None`` (never
      ``0``) whenever there is nothing to measure — a zero would assert we
      measured and found no exposure.
    """

    verification: SettlementVerification
    realized_fx_gain_loss: Decimal | None = None

    @property
    def is_discrepancy(self) -> bool:
        return self.verification.is_discrepancy

    def as_audit_details(self) -> dict:
        """The ``audit_log.details`` fragment for a completion.

        PII-free and exact: money serialises as a decimal STRING, never a
        float. Written on EVERY completion — matched, mismatched and
        unverified alike — so a rail that reports no amount is a visible
        blind spot rather than a silent one. The FX key is absent, not zero,
        when there is no exposure to report.
        """
        details: dict = {"settlement": self.verification.as_details()}
        if self.realized_fx_gain_loss is not None:
            details["realized_fx_gain_loss"] = str(self.realized_fx_gain_loss)
        return details


async def record_completion(
    db: AsyncSession,
    *,
    payment: Payment,
    adapter,
    invoice: Invoice | None,
    reported_amount: Decimal | None = None,
    reported_currency: str | None = None,
) -> SettlementCompletion:
    """Book everything a payment reaching ``completed`` owes the record.

    The ONE entry point for both completion paths — ``api/payments``'s webhook
    handler and ``services/payment_reconciler``'s backstop poll. It exists as a
    single function rather than a pair of calls each caller makes because the
    pair had already drifted once: realized FX was computed inline on the
    webhook path only, so a cross-currency payment the backstop recovered — the
    case with the least evidence, by construction — booked no realized gain or
    loss at all, and nothing downstream re-derives it (the webhook handler
    refuses an already-terminal payment, so a late webhook could not supply it
    either).

    Does not commit, does not open exceptions and does not write the audit row:
    the caller owns those, because the webhook additionally has to decide
    whether to capture an early-pay discount off the same verdict.
    """
    verification = await record_settlement(
        db,
        payment=payment,
        adapter=adapter,
        invoice=invoice,
        reported_amount=reported_amount,
        reported_currency=reported_currency,
    )
    realized_fx: Decimal | None = None
    if invoice is not None:
        # Realized FX gain/loss, at the moment a foreign-currency invoice
        # actually settles. The liability was accrued at the rate locked on the
        # invoice when its reporting amount was materialized; what moved is
        # `source_amount` in the home currency, and the difference is realized
        # here and nowhere else. The helper returns `None` — never a zero — for
        # a domestic payment, an invoice with no accrual rate, a same-currency
        # settlement, or an accrual and an outflow denominated differently.
        realized_fx = realized_fx_gain_loss_for_settlement(
            invoice_amount=invoice.amount,
            invoice_currency=invoice.currency,
            reporting_currency=invoice.reporting_currency,
            reporting_fx_rate=invoice.reporting_fx_rate,
            paid_source_amount=payment.source_amount,
            paid_source_currency=payment.source_currency,
        )
    return SettlementCompletion(verification=verification, realized_fx_gain_loss=realized_fx)


async def open_settlement_mismatch_exception(
    db: AsyncSession,
    *,
    payment: Payment,
    invoice: Invoice | None,
    org: Organization,
    verification: SettlementVerification,
) -> None:
    """Raise a payment-blocking `fraud_flag` for a settlement that didn't
    reconcile against what AP authorized.

    `fraud_flag` deliberately, not a new taxonomy entry: it is exactly what
    `api/positive_pay.py` raises for an ALTERED cheque — a payment instrument
    presented at an amount we never wrote — and this is the electronic
    equivalent, caught days earlier on rails where no cheque exists. It is
    also in `PAYMENT_BLOCKING_EXCEPTION_TYPES`, so until a human resolves it
    the invoice can't be swept into another payment run (which matters the
    moment this payment is voided and the invoice returns to `approved`).

    Dedupes on `(invoice_id, fraud_flag, open|escalated)` — the same rule
    Positive Pay's own return processing uses. One open fraud flag on an
    invoice is the signal; piling on a second adds noise, not information.
    The description is PII-free (amounts, currency codes and the verdict —
    the row already carries the invoice FK).
    """
    if invoice is None:
        # A payment whose invoice row is gone still gets the audit row; there
        # is no queue entry to attach the flag to.
        return
    from app.services.exception_service import create_exception

    already = (
        await db.execute(
            select(func.count()).where(
                APException.invoice_id == invoice.id,
                APException.exception_type == "fraud_flag",
                APException.status.in_(["open", "escalated"]),
            )
        )
    ).scalar() or 0
    if already > 0:
        return

    finding = discrepancy_finding(verification)
    await create_exception(
        db,
        exception_type="fraud_flag",
        description=finding["message"],
        description_code=finding["code"],
        description_params=finding["params"],
        organization_id=org.id,
        severity="error",
        invoice=invoice,
        # The rail settled for an amount that does not match what we booked.
        # The discrepancy is the processor's, arriving on a webhook — no human
        # act to have a second person look at.
        raised_by_user_id=None,
    )


__all__ = [
    "SettlementCompletion",
    "open_settlement_mismatch_exception",
    "record_completion",
    "record_settlement",
]


# ── A run's virtual-card leg (decisions §537, §543) ───────────────────────
#
# A run's card leg books a `completed` `Payment` the moment the card is MINTED
# (`api/payments._execute_single_payment`), but money moves only when the vendor
# charges the card — and the card webhook recorded that charge on the
# `VirtualCard` row alone. With `settled_amount` NULL, `settlement_coverage`
# failed OPEN, so the payment sync and an ERP `paid` report both closed the
# invoice while the card was still spendable. These two functions are the whole
# fix: the card's charge is written onto the payment it pays, and coverage of a
# card leg asks the card. Only a SETTLED charge counts (§543): an authorization
# can still be reversed or expire, so it leaves the leg in flight.

CARD_LEG_METHOD = "virtual_card"
#: The card status that means the vendor's charge CLEARED — money moved. The
#: card webhook sets it on a settlement event; ``charged`` is only the
#: authorization, a hold that can still be reversed or expire (decisions §543).
CARD_SETTLED_STATUS = "completed"


async def card_leg_card(db: AsyncSession, payment: Payment):
    """The card a run's card leg paid with, preferring one that was charged.

    The card the leg minted names the payment (`virtual_cards.payment_id`); a
    leg that converged onto an unlinked `/cards/generate` card links it too.
    The invoice's still-unlinked card is read as a fallback, so a leg never
    loses sight of the card that pays its invoice.
    """
    from sqlalchemy import case, or_

    from app.models.virtual_card import VirtualCard
    from app.services.card_issuance import CARD_SPENT_STATUSES

    clauses = [VirtualCard.payment_id == payment.id]
    if payment.invoice_id is not None:
        clauses.append(
            (VirtualCard.invoice_id == payment.invoice_id) & VirtualCard.payment_id.is_(None)
        )
    spent_first = case((VirtualCard.status.in_(CARD_SPENT_STATUSES), 0), else_=1)
    return (
        await db.execute(
            select(VirtualCard)
            .where(or_(*clauses))
            .order_by(spent_first, VirtualCard.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def payment_coverage(db: AsyncSession, payment: Payment, invoice_currency: str | None):
    """``settlement_coverage`` for one payment, honest about a card leg.

    Every caller that decides whether a payment discharges its invoice goes
    through here — the payment sync (`payment_erp_sync`) and an ERP's `paid`
    report (`erp_status_report`, webhook and poll alike) — so they cannot
    disagree about a card leg.

    A card leg counts only a SETTLED charge (decisions §543). Its card is read
    every time: not yet settled (``created`` / ``sent`` / ``active``, merely
    authorized as ``charged``, cancelled, or no card found) is ``in_flight``,
    whatever the payment carries, because an authorization is a hold the
    network can still reverse or let expire — no money has moved. Settled
    (``completed``), the figure the webhook stamped on the payment
    (:func:`record_card_spend`) is judged like any rail's, falling back to the
    card's own ``amount_charged`` when nothing was stamped.
    """
    from app.services.payment_settlement import settlement_coverage

    settled = payment.settled_amount
    settled_currency = payment.settled_currency
    unstorable = bool(payment.settled_amount_unstorable)
    awaiting = False
    if payment.method == CARD_LEG_METHOD:
        card = await card_leg_card(db, payment)
        if card is None or card.status != CARD_SETTLED_STATUS:
            # Not money yet: a figure an earlier build stamped at the
            # authorization (§537) is not evidence of a settled charge either.
            awaiting = True
            settled, settled_currency, unstorable = None, None, False
        elif settled is None and not unstorable:
            settled, unstorable = persistable_settled_amount(card.amount_charged)
            settled_currency = card.currency
    return settlement_coverage(
        settled_amount=settled,
        settled_currency=settled_currency,
        target_amount=payment.amount,
        target_currency=invoice_currency,
        source_amount=payment.source_amount,
        source_currency=payment.source_currency,
        settled_amount_unstorable=unstorable,
        awaiting_card_spend=awaiting,
    )


async def record_card_spend(db: AsyncSession, card) -> Payment | None:
    """Write a card's SETTLED charge onto the run payment it pays; return it.

    Called by the card webhook on settlement only (decisions §543): an
    authorization is a hold, not money moved — the network can reverse it or
    let it expire, and a card provider sends those as their own events — so the
    leg stays ``in_flight`` (:func:`payment_coverage`) until the charge clears.
    Then ``payments.settled_amount`` is what the vendor actually took: a short
    charge holds the invoice exactly as a short ACH does, and
    ``/settlement/accept`` and ``/void`` are its exits.

    Returns ``None`` — nothing written — for a card not yet settled, a card no
    run payment owns (``/cards/generate``), a non-card or non-completed
    payment, or a charge with no amount. Idempotent: the same card writes the
    same figure.
    """
    if card.payment_id is None or card.amount_charged is None or card.status != CARD_SETTLED_STATUS:
        return None
    payment = await db.get(Payment, card.payment_id)
    if payment is None or payment.method != CARD_LEG_METHOD or payment.status != "completed":
        return None
    amount, unstorable = persistable_settled_amount(card.amount_charged)
    payment.settled_amount = amount
    payment.settled_currency = card.currency
    payment.settled_amount_unstorable = unstorable
    return payment


#: `Exception.erp_report["kind"]` of the flag a card charged past what it pays
#: opens (decisions §554); deduped by kind like every reconciliation flag (§535).
CARD_OVER_SETTLED_KIND = "card_over_settled"


async def flag_card_over_settlement(db: AsyncSession, card, payment: Payment | None) -> bool:
    """Open a payment-blocking reconciliation flag when a settled card's total
    cleared figure exceeds what it pays; return whether one was opened.

    Decisions §554. A card can clear more than once (an incremental capture, a
    tip, a force post past the limit), and over-settlement is ``covered`` by
    design (``settlement_coverage``), so without this nothing would tell a
    person the vendor took more than AP authorized. The target is the run
    payment's amount when a payment owns the card, else the invoice's. The
    verdict is ``verify_settlement``'s, with its tolerance and currency rule:
    a card in another currency than the invoice is not judged here (the
    settlement verdict for a cross-currency leg is the rail's, not the card's).
    The finding is the catalogue's ``settlement_amount_over``; the exception is
    deduped by kind (``card_over_settled``), so each further clearing past the
    figure does not pile up rows. Does not commit.
    """
    from app.services.erp_status_sync import (
        PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
        open_erp_reconciliation_exception,
    )
    from app.services.payment_settlement import OUTCOME_AMOUNT_MISMATCH

    if card.amount_charged is None or card.status != CARD_SETTLED_STATUS:
        return False
    invoice = await db.get(Invoice, card.invoice_id)
    if invoice is None:
        return False
    target = payment.amount if payment is not None else invoice.amount
    verification = verify_settlement(
        reported_amount=card.amount_charged,
        reported_currency=card.currency,
        target_amount=target,
        target_currency=invoice.currency,
        source_amount=payment.source_amount if payment is not None else None,
        source_currency=payment.source_currency if payment is not None else None,
    )
    if verification.outcome != OUTCOME_AMOUNT_MISMATCH or not (
        verification.variance is not None and verification.variance > 0
    ):
        return False
    logger.warning("Card settled past the amount it pays; flagged for reconciliation")
    return await open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=card.organization_id,
        finding=discrepancy_finding(verification),
        exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
        erp_report={
            "kind": CARD_OVER_SETTLED_KIND,
            "virtual_card_id": str(card.id),
            "settled_amount": str(card.amount_charged),
            "authorized_amount": str(verification.authorized_amount),
            "currency": card.currency,
        },
    )
