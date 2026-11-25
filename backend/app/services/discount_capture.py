"""The bridge between an accepted early-pay discount and the payment that
takes it: what a payment run deducts (``applicable_discounts``), recognizing
the settlement that realized it (``capture_offer_for_settled_payment``), and
un-realizing it when that payment is voided
(``reverse_captures_for_voided_payment``).

**What is paid.** Once a supplier's offer is accepted, the buyer pays the
discounted amount before the offer's deadline — that is the whole bargain of
dynamic discounting. ``applicable_discounts`` is the one answer to "does an
accepted discount apply to this invoice if it is paid on this date, and for
how much"; ``payment_runs.payable_amounts`` deducts it, the run books it on the
payment (``Payment.discount_offer_id`` / ``discount_amount``), and dispatch
re-asks on the day the money moves. A missed deadline pays the full amount.

**What is captured.** The rest of this docstring describes the capture leg —
the missing caller for ``discount_offers.mark_captured``.

``services/discount_offers.mark_captured`` is the only code that sets
``DiscountOffer.captured_amount`` / ``captured_at`` and transitions an offer
``accepted -> captured``, but it is a pure mutator: something has to notice
"this payment settling this invoice IS the discounted payoff" and call it.
This module is that something. It is invoked from every place a ``Payment``
reaches ``completed`` against an invoice — the synchronous adapter/card leg
in ``app/api/payments.py::_execute_single_payment`` and the async
webhook-driven completion in ``app/api/payments.py::payment_webhook`` — so a
discount is recognized whether the rail confirms instantly (mock, virtual
card) or days later (ACH/wire via a processor webhook).

Money is ``Decimal`` throughout; this module does its own DB query but never
commits — same convention as the rest of the payment path (the caller owns
the transaction).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_memo import CreditMemo
from app.models.discount import (
    OFFER_SCOPE_INVOICE,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_CAPTURED,
    DiscountOffer,
)
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.services import discount_offers as offers_svc

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class AppliedDiscount:
    """An accepted early-payment discount a payment made on ``pay_date`` takes.

    ``amount`` is the exact savings deducted — ``discount_savings(base_amount,
    accepted_tier)``, cent-quantized half-up — and ``deadline`` the last day it
    could still be taken (``discount_offers.accepted_discount_deadline``).
    """

    offer_id: uuid.UUID
    amount: Decimal
    deadline: date


async def applicable_discounts(
    db: AsyncSession,
    invoices: Iterable[Invoice],
    *,
    pay_date: date,
    net_amounts: dict[uuid.UUID, Decimal],
) -> dict[uuid.UUID, AppliedDiscount]:
    """Which of ``invoices`` an ACCEPTED early-pay discount applies to if paid
    on ``pay_date`` — and for how much. Two queries for the whole batch at
    most (the offers, then the invoices whose applied memo already took one).

    An offer applies only when every one of these holds, and otherwise the
    invoice is paid in full (never refused — the supplier is owed the whole
    amount once the bargain cannot be honoured):

    * it is invoice-scoped and ``accepted`` with a tier on record. A
      vendor-scoped bulk offer spans several invoices, so no single payment can
      be shown to be its settlement — the same reason the capture leg skips it;
    * ``pay_date`` is on or before the tier's deadline
      (``accepted_discount_deadline``). An offer the payment path cannot date
      is not applied — a deduction the supplier may consider expired is a
      short payment;
    * its ``currency`` is the invoice's (a payment is denominated in the
      invoice's currency; a figure in another one is not a deduction from it).
      An invoice that records no currency takes no discount;
    * its ``base_amount`` is still the invoice's ``amount``. An invoice whose
      amount moved after the offer was made no longer matches the terms the
      supplier accepted, and recomputing them would be pricing a bargain nobody
      struck; the invoice is paid in full and the mismatch logged;
    * the deduction leaves something to pay: ``savings < net_amounts[id]``
      (the invoice net of applied credit memos);
    * the discount has not ALREADY been taken through a credit memo: no
      applied memo on the invoice names a discount offer
      (``credit_memos.discount_offer_id``, decisions §520). Such a memo is
      already inside ``net_amounts``, so deducting the offer again would
      short-pay the supplier by it; the settlement captures the offer through
      that memo instead (:func:`capture_offers_for_settled_payment`). A memo
      that names no offer is an ordinary credit whatever its amount — a return
      credit that happens to equal the savings does not cost the buyer the
      discount (it used to: §250 matched on the amount).

    Several accepted offers on one invoice (a re-sent offer, both accepted):
    the earliest accepted is the one taken — one payment realizes one discount,
    the rule the capture leg applies.
    """
    rows = [inv for inv in invoices if inv.id is not None]
    if not rows:
        return {}
    offers = (
        (
            await db.execute(
                select(DiscountOffer)
                .where(
                    DiscountOffer.invoice_id.in_([inv.id for inv in rows]),
                    DiscountOffer.scope == OFFER_SCOPE_INVOICE,
                    DiscountOffer.status == OFFER_STATUS_ACCEPTED,
                )
                .order_by(DiscountOffer.accepted_at.asc().nulls_last(), DiscountOffer.id.asc())
            )
        )
        .scalars()
        .all()
    )
    by_invoice: dict[uuid.UUID, list[DiscountOffer]] = {}
    for offer in offers:
        by_invoice.setdefault(offer.invoice_id, []).append(offer)
    # Invoices that already took a discount through an applied credit memo
    # linked to an offer (decisions §520). Skipped entirely (no query) when no
    # invoice has an accepted offer.
    taken_by_memo: set[uuid.UUID] = set()
    if by_invoice:
        taken_by_memo = set(
            (
                await db.execute(
                    select(CreditMemo.invoice_id).where(
                        CreditMemo.invoice_id.in_(list(by_invoice)),
                        CreditMemo.status == "applied",
                        CreditMemo.discount_offer_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    out: dict[uuid.UUID, AppliedDiscount] = {}
    for inv in rows:
        if inv.id in taken_by_memo:
            # One invoice, one discount: the memo took it, whichever offer it
            # names, so no other accepted offer comes off on top.
            logger.info(
                "invoice %s already took its discount through a linked credit memo; "
                "not deducting it again",
                inv.id,
            )
            continue
        for offer in by_invoice.get(inv.id, ()):
            if not offer.accepted_tier:
                continue
            deadline = offers_svc.accepted_discount_deadline(offer)
            if deadline is None or pay_date > deadline:
                continue
            # An invoice with no currency cannot be shown to share the offer's.
            if not inv.currency or (offer.currency or "").upper() != inv.currency.upper():
                continue
            base = Decimal(offer.base_amount).quantize(_CENTS)
            if inv.amount is None or base != Decimal(inv.amount).quantize(_CENTS):
                logger.warning(
                    "discount offer %s base no longer matches its invoice's amount; "
                    "paying the invoice in full",
                    offer.id,
                )
                continue
            savings = offers_svc.discount_savings(base, offer.accepted_tier)
            if savings <= 0 or savings >= net_amounts.get(inv.id, Decimal("0")):
                continue
            out[inv.id] = AppliedDiscount(offer_id=offer.id, amount=savings, deadline=deadline)
            break
    return out


async def capture_offer_for_settled_payment(
    db: AsyncSession,
    *,
    payment: Payment,
    invoice_currency: str,
    now: datetime,
) -> list[DiscountOffer]:
    """Capture the discount ``payment`` took, now that it has settled.

    A payment booked with a discount (``payment.discount_offer_id``) names its
    offer, so the capture is that offer, for exactly ``payment.discount_amount``
    — no matching, no guessing — and is stamped with the payment's id so a void
    reverses exactly this capture. Idempotent: an offer already ``captured``
    (a retried webhook, a reconciliation re-run) is left untouched, and the row
    is locked so two settlement paths cannot both capture it.

    A payment with no discount booked falls back to the credit memo that took
    the discount (:func:`capture_offers_for_settled_payment`): the payment was
    netted of that memo, so settling it realizes the offer the memo names.
    """
    if payment.discount_offer_id is None:
        return await capture_offers_for_settled_payment(
            db,
            invoice_id=payment.invoice_id,
            invoice_currency=invoice_currency,
            now=now,
            payment_id=payment.id,
        )
    offer = (
        await db.execute(
            select(DiscountOffer)
            .where(DiscountOffer.id == payment.discount_offer_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if offer is None or offer.status != OFFER_STATUS_ACCEPTED:
        return []
    offers_svc.mark_captured(
        offer,
        captured_amount=payment.discount_amount or Decimal("0"),
        now=now,
        payment_id=payment.id,
    )
    return [offer]


async def capture_offers_for_settled_payment(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    invoice_currency: str,
    now: datetime,
    payment_id: uuid.UUID | None = None,
) -> list[DiscountOffer]:
    """Capture the ``accepted`` invoice-scoped offer an applied credit memo on
    ``invoice_id`` took (``credit_memos.discount_offer_id``), for that memo's
    amount — the deduction the supplier actually gave.

    The FALLBACK leg of :func:`capture_offer_for_settled_payment`, for a
    payment that booked no discount of its own. ``payment_id`` is stamped on
    the capture when given, so a void reverses exactly this capture.

    **Only a link counts, never an amount** (decisions §520). This leg used to
    capture any accepted offer whose discounted payoff equalled the payment
    amount, so a payment netted of a return credit that happened to equal the
    savings recorded the offer as captured — realized savings on the CFO
    dashboard for a discount nobody took. A payment with no linked memo
    captures nothing; the offer stays ``accepted``.

    Only invoice-scoped offers are considered — a vendor-scoped bulk offer
    spans several invoices, so no single invoice's settlement is it; the memo
    create path refuses to link one anyway.

    **Currency is checked.** An offer's ``currency`` can diverge from its
    invoice's (``POST /api/discounts/offers`` takes an explicit one), and
    linking refuses that, but a capture across currencies would misreport the
    savings, so it is re-checked here rather than trusted.

    Idempotent: only offers currently ``accepted`` are read, and they are
    locked, so a retry or a concurrent settlement path finds nothing left to
    capture. One settlement realizes at most one discount: should two memos
    on one invoice each name an offer, the earliest accepted offer is captured
    and the scan stops.

    Returns the offers captured — empty in the common no-discount case.
    """
    rows = (
        await db.execute(
            select(DiscountOffer, CreditMemo.amount)
            .join(CreditMemo, CreditMemo.discount_offer_id == DiscountOffer.id)
            .where(
                CreditMemo.invoice_id == invoice_id,
                CreditMemo.status == "applied",
                DiscountOffer.invoice_id == invoice_id,
                DiscountOffer.scope == OFFER_SCOPE_INVOICE,
                DiscountOffer.status == OFFER_STATUS_ACCEPTED,
            )
            .order_by(DiscountOffer.accepted_at.asc().nulls_last(), DiscountOffer.id.asc())
            .with_for_update(of=DiscountOffer)
        )
    ).all()

    invoice_ccy = (invoice_currency or "").upper()
    for offer, memo_amount in rows:
        if (offer.currency or "").upper() != invoice_ccy:
            logger.warning(
                "discount offer %s currency (%s) does not match its invoice's "
                "currency (%s); not capturing it",
                offer.id,
                offer.currency,
                invoice_currency,
            )
            continue
        try:
            offers_svc.mark_captured(
                offer, captured_amount=memo_amount, now=now, payment_id=payment_id
            )
        except ValueError:
            # Lost a race with another settlement path that captured it first.
            logger.info("discount offer %s already captured; skipping duplicate capture", offer.id)
            continue
        # One payment, one discount.
        return [offer]
    return []


async def reverse_captures_for_voided_payment(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    voided_payment_id: uuid.UUID,
    previous_status: str | None,
) -> list[tuple[DiscountOffer, Decimal]]:
    """Un-capture the discount a now-voided payment had realized.

    The inverse of :func:`capture_offer_for_settled_payment`, called from
    ``POST /api/payments/{id}/void``. A void returns the invoice to
    ``approved`` — nothing was paid, so nothing was saved — but the offer used
    to stay ``captured`` with its ``captured_amount``: the discounting
    dashboard kept reporting realized savings on an invoice that is now unpaid,
    and a re-payment at the discounted payoff could capture nothing, because
    the offer was no longer ``accepted``.

    Which offer did THIS payment capture? Every capture made since migration
    0104 names its payment (``captured_by_payment_id``), so those are reversed
    exactly — whatever else the invoice carries. Only a capture made before
    that column existed (NULL) falls back to attribution by elimination, and
    both conditions must then hold:

    * ``previous_status == "completed"`` — capture only ever runs when a
      payment reaches ``completed`` (both legs in ``api/payments``), so voiding
      an in-flight one cannot have realized anything.
    * no OTHER ``completed`` payment remains on the invoice. With one live
      settlement, every ``captured`` invoice-scoped offer on the invoice is the
      one it realized (an earlier, voided settlement already reversed its own).
      If another completed payment survives, the capture may be its, and
      guessing would un-realize savings that really happened — so the offer is
      left alone.

    Each reversed offer goes back to ``accepted`` via
    :func:`discount_offers.reverse_capture` and is returned with the amount
    reversed, for the caller's audit row. Never commits — the void's
    transaction owns that, so the reversal lands atomically with the void.
    """
    exact = (
        (
            await db.execute(
                select(DiscountOffer)
                .where(
                    DiscountOffer.captured_by_payment_id == voided_payment_id,
                    DiscountOffer.status == OFFER_STATUS_CAPTURED,
                )
                .order_by(DiscountOffer.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if exact:
        return [(offer, offers_svc.reverse_capture(offer)) for offer in exact]

    if previous_status != "completed":
        return []

    other_settled = (
        await db.execute(
            select(Payment.id)
            .where(
                Payment.invoice_id == invoice_id,
                Payment.id != voided_payment_id,
                Payment.status == "completed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if other_settled is not None:
        return []

    offers = (
        (
            await db.execute(
                select(DiscountOffer)
                .where(
                    DiscountOffer.invoice_id == invoice_id,
                    DiscountOffer.scope == OFFER_SCOPE_INVOICE,
                    DiscountOffer.status == OFFER_STATUS_CAPTURED,
                    # Elimination is for captures that name no payment. One
                    # that names another payment is that payment's to reverse.
                    DiscountOffer.captured_by_payment_id.is_(None),
                )
                .order_by(DiscountOffer.id.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    reversed_: list[tuple[DiscountOffer, Decimal]] = []
    for offer in offers:
        reversed_.append((offer, offers_svc.reverse_capture(offer)))
    return reversed_
