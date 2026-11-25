"""Apply one ERP status report to one invoice — the generic route's transition.

The generic ERP webhook (``POST /api/erp/webhook/{erp_type}``) and the status
polling sweep (``services/erp_status_poll_sweep``, decisions §528) both end
here, so a status the ERP reports moves the invoice the same way whichever
channel carried it. The caller resolves the invoice, holds its row lock
(``workflow_engine.get_invoice_for_update``) and commits when the outcome is
not :data:`NOOP`; this module never commits.

Only the authoritative state machine (``workflow_engine.VALID_TRANSITIONS``)
decides which edges are taken. There is deliberately no second local
transition map: a divergent copy that permitted an edge the engine forbids
would make ``transition_invoice`` raise a 409 on a path that must answer
silently.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.services.erp_status_sync import (
    ERP_PAID_RECORDABLE,
    ERP_REPORT_PAID_WHILE_SCHEDULED,
    ERP_REPORT_SETTLED_SHORT,
    ERP_REPORT_VOIDED_AFTER_POST,
    PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
    erp_finding,
    withdraw_erp_paid_reports,
)
from app.services.erp_status_sync import (
    open_erp_reconciliation_exception as _open_erp_reconciliation_exception,
)
from app.services.erp_status_sync import (
    record_erp_reported_payment as _record_erp_reported_payment,
)
from app.services.invoice_warning_catalog import exception_finding
from app.services.payment_settlement import COVERAGE_IN_FLIGHT
from app.services.payment_settlement_record import payment_coverage
from app.services.workflow_engine import VALID_TRANSITIONS, transition_invoice

# Map ERP status strings to our internal status transitions.
ERP_STATUS_MAP = {
    # Merge.dev statuses
    "OPEN": InvoiceStatus.posted_in_erp,
    "SUBMITTED": InvoiceStatus.posted_in_erp,
    # Business Central
    "Open": InvoiceStatus.posted_in_erp,
    "Paid": InvoiceStatus.paid,
    # NetSuite
    "open": InvoiceStatus.posted_in_erp,
    "paidInFull": InvoiceStatus.paid,
    # Generic
    "posted": InvoiceStatus.posted_in_erp,
    "paid": InvoiceStatus.paid,
    "cancelled": InvoiceStatus.failed,
    "voided": InvoiceStatus.failed,
}

#: :func:`apply_erp_status_report` outcomes. Everything but ``NOOP`` changed
#: the session and must be committed by the caller. A flag a live row of the
#: same kind already raised changes nothing, so it is ``NOOP`` (or
#: ``WITHDRAWN``), never counted as applied.
NOOP = "noop"
TRANSITIONED = "transitioned"
PAYMENT_RECORDED = "payment_recorded"
EXCEPTION_OPENED = "exception_opened"
WITHDRAWN = "withdrawn"
HELD_SETTLED_SHORT = "held_settled_short"
HELD_PAID_WHILE_SCHEDULED = "held_paid_while_scheduled"


async def apply_erp_status_report(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id: uuid.UUID,
    org,
    erp_type: str,
    erp_status: str,
    target_status: InvoiceStatus,
    erp_document_id: str | None,
    event_id: object = None,
    paid_on: date | None = None,
    payment_method: str | None = None,
    source: str | None = None,
) -> str:
    """Apply ``erp_status`` (already mapped to ``target_status``) to the locked
    ``invoice``. Returns one of the outcome constants above.

    ``source`` marks the audit row of a transition when the report did not come
    from the webhook (``"poll"`` for the sweep); the webhook passes nothing, so
    its rows are unchanged.
    """
    current = invoice.status
    # Any status but `Paid` for an invoice still payable here means the ERP no
    # longer shows the bill paid (a voided cheque reads `Open`): a payment it
    # reported earlier, which FeohLedger could not record, must never be
    # recorded now (decisions §319). Before the transition screen, because
    # `posted_in_erp → posted_in_erp` is a silent no-op below.
    withdrawn = 0
    if target_status is not InvoiceStatus.paid and current in ERP_PAID_RECORDABLE:
        withdrawn = await withdraw_erp_paid_reports(
            db, invoice, erp_type=erp_type, erp_state=erp_status
        )
    if target_status not in VALID_TRANSITIONS.get(current, set()):
        # The state machine forbids this transition for the invoice's current
        # state. Almost always a no-op — a stale or duplicate ERP status for an
        # invoice that already moved on. ONE forbidden case is a real
        # reconciliation signal we must NOT drop: the ERP reports the invoice
        # VOIDED/CANCELLED (→ failed) after we already advanced it. Money may
        # already be in flight, so we never auto-transition (auto → failed from
        # payment_scheduled/paid would collide with the money path); instead we
        # open an Exception for a human to reconcile. Every OTHER forbidden
        # transition stays a pure no-op — turning them all into exceptions
        # would be noise.
        if target_status is InvoiceStatus.failed:
            opened = await raise_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org_id,
                erp_type=erp_type,
                erp_status=erp_status,
                erp_document_id=erp_document_id,
                event_id=event_id,
            )
            if opened:
                return EXCEPTION_OPENED
            return WITHDRAWN if withdrawn else NOOP
        if target_status is InvoiceStatus.paid and current in ERP_PAID_RECORDABLE:
            # The ERP paid an invoice FeohLedger never paid — the ERP-led half
            # of the no-rail model (issue #517). The state machine has no
            # `posted_in_erp → paid` edge because `paid` needs a payment behind
            # it; recording the ERP's payment supplies one, through the same
            # service a user's "paid outside FeohLedger" goes through.
            await _record_erp_reported_payment(
                db,
                invoice,
                org=org,
                erp_type=erp_type,
                erp_document_id=erp_document_id,
                event_id=event_id,
                paid_on=paid_on,
                method=payment_method,
            )
            return PAYMENT_RECORDED
        return WITHDRAWN if withdrawn else NOOP

    # `payment_scheduled → paid` is a legal edge, but it is the exact
    # transition the settlement-coverage hold governs. `payment_erp_sync` (the
    # normal, one-shot path to `paid`) refuses to close out an invoice whose
    # rail settled SHORT of what AP authorized — it holds at
    # `payment_scheduled` for a human, with `/settlement/accept` and `/void` as
    # the two exits. A paid report from the tenant's own ERP (which
    # legitimately fires once a bill payment is applied there) must not walk
    # around that: it would flip a short-paid invoice to `paid`, fire the
    # outbound `payment.settled` webhook, email the supplier, and count the full
    # amount in aging / dashboard / 1099 YTD — and `/settlement/accept` would
    # then 409, so the documented exit is gone. Run the SAME check off the SAME
    # persisted figure (`Payment.settled_amount`) and take the
    # reconciliation-exception path on a non-covering verdict.
    #
    # With NO completed payment the ERP's `paid` is not ours at all: this route
    # and the polling sweep serve adapters `payment_erp_sync` writes nothing
    # back to, so the bill was paid in the ERP while our own rail payment (or a
    # live virtual card, which books no `Payment`) is still in flight. Moving
    # to `paid` would hide the double payment and walk around the coverage
    # hold above. Hold instead, behind a payment-blocking exception, as the
    # QuickBooks sync does (`apply_bill_report`, decisions §533) — but in words
    # of its own: this route cannot tell an outside payment from a bookkeeper
    # recording our own in-flight one, so it never says to void one (§535).
    if target_status is InvoiceStatus.paid and current == InvoiceStatus.payment_scheduled:
        settling = (
            await db.execute(
                select(Payment)
                .where(
                    Payment.invoice_id == invoice.id,
                    Payment.status == "completed",
                )
                .order_by(Payment.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        # A run's card leg is `completed` once its card is minted; uncharged,
        # it is as in flight as a pending ACH (decisions §537).
        coverage = (
            None if settling is None else await payment_coverage(db, settling, invoice.currency)
        )
        if settling is None or coverage.state == COVERAGE_IN_FLIGHT:
            report = {
                "kind": ERP_REPORT_PAID_WHILE_SCHEDULED,
                "erp_type": erp_type,
                "erp_document_id": erp_document_id,
                "event_id": str(event_id) if event_id else None,
                "invoice_status": current.value,
            }
            if source is not None:
                report["source"] = source
            opened = await _open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org_id,
                exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
                finding=exception_finding("erp_paid_while_payment_in_flight"),
                erp_report=report,
            )
            return HELD_PAID_WHILE_SCHEDULED if opened else NOOP
        if not coverage.completes_invoice:
            opened = await _open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=org_id,
                finding=erp_finding("erp_settled_short", erp_type),
                erp_report={
                    "kind": ERP_REPORT_SETTLED_SHORT,
                    "erp_type": erp_type,
                    "erp_document_id": erp_document_id,
                    "event_id": str(event_id) if event_id else None,
                    "payment_id": str(settling.id),
                    "coverage_state": coverage.state,
                    "shortfall": (
                        str(coverage.shortfall) if coverage.shortfall is not None else None
                    ),
                },
            )
            return HELD_SETTLED_SHORT if opened else NOOP

    # PII guard: never splat the raw ERP `details` payload into the append-only
    # audit row — the ERP may include vendor bank/tax/address fields.
    # Whitelist only the safe, non-PII routing identifiers.
    details = {
        "erp_type": erp_type,
        "erp_status": erp_status,
        "erp_document_id": erp_document_id,
        "raw_event_id": str(event_id) if event_id else None,
    }
    if source is not None:
        details["source"] = source
    await transition_invoice(
        db,
        invoice,
        target_status,
        action_name=f"invoice.erp_status_{target_status.value}",
        details=details,
    )
    return TRANSITIONED


async def raise_erp_reconciliation_exception(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    erp_type: str,
    erp_status: str,
    erp_document_id: str | None,
    event_id,
) -> bool:
    """Open an ``erp_reconciliation`` Exception when the ERP reports an invoice
    VOIDED/CANCELLED (``→ failed``) that we've already advanced past the point
    where ``→ failed`` is a legal transition (``sent_to_erp`` /
    ``posted_in_erp`` / ``payment_scheduled`` / ``paid``). Money may already be
    in flight, so this is a review signal — we deliberately do NOT
    auto-transition the invoice. (A payment the ERP reported earlier and
    FeohLedger could not record has already been withdrawn by
    :func:`apply_erp_status_report`, which does so for every non-`Paid`
    status — decisions §319.) Idempotent while one of the same kind is open or
    escalated (decisions §533). Returns whether one was opened.
    """
    return await _open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=org_id,
        finding=erp_finding("erp_voided_after_post", erp_type),
        erp_report={
            "kind": ERP_REPORT_VOIDED_AFTER_POST,
            "erp_type": erp_type,
            "erp_status": erp_status,
            "erp_document_id": erp_document_id,
            "event_id": str(event_id) if event_id else None,
            "invoice_status": invoice.status.value,
        },
    )
