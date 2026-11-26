"""A person's "this bill is this invoice's" for a Xero push refused
``erp_bill_key_shared`` (decisions §572, §587).

Xero's bill key is supplier + supplier invoice number. When another invoice in
the tenant carries the same pair and a live bill sits on it, the push refuses
to adopt the bill (it may be the other invoice's) and refuses to create a
second one beside it. When the bill really is this invoice's (an earlier
attempt's response was lost, and the sharer is, say, a rejected duplicate) the
push kept refusing until someone renumbered an invoice, which weakens
duplicate detection on a real supplier number.

The confirmation is the way out, and it decides nothing the push does not
re-check:

1. :func:`confirm_lookup_refusal` is the one verdict: served on ``GET
   /review-actions`` as ``erp_bill_confirm_refusal`` only on a failed push
   whose last refusal was ``erp_bill_key_shared``, and raised from by both
   endpoints.
2. The bill is named by the adapter's own read-only lookup (``GET
   /erp-bill-confirmation``), run without the shared-key refusal the orphan
   lookup applies: that refusal exists because the lookup feeds a void, and
   here the person reads the bill in order to say whose it is.
3. ``POST /confirm-erp-bill`` re-runs the lookup (no lock across the ERP
   call), then under the invoice and instance locks re-checks the verdict,
   that the lookup still names that very bill, and that no other invoice
   holds it (:func:`claimed_elsewhere`, serialized per bill by an advisory
   lock). It records ``{document_id, actor, reason, at}`` under
   :data:`CONFIRMED_BILL_KEY` on the invoice's workflow state, writes an
   append-only ``invoice.erp_bill_confirmed`` row, and starts the ordinary
   retry. The push hands the id to the adapter
   (``InvoicePayload.confirmed_bill_id``), and Xero adopts the found bill on a
   shared key only when it is that id, at the approved total as any adoption.
   So the bill reaches the invoice through the same success path an adoption
   always took: ``failed → sending_to_erp → sent_to_erp → posted_in_erp |
   done``, ``erp_document_id`` and the URL set by ``_call_erp``.

**One bill, one invoice.** The claim is recorded on the confirming invoice,
not on the sharer, and every confirmation reads every other invoice's claims:
an invoice whose ``erp_document_id`` is the bill, whose state records it as
the bill its own failed push left (``erp_orphan_document_id``), or whose state
records a confirmation of it. The sharer's own push still refuses (its key is
shared and it confirmed nothing), and its confirmation answers
``erp_bill_claimed``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceStatus

#: ``WorkflowInstance.state_data`` key: the coded refusal of the push's last
#: failure (``services/erp`` writes it as it lands the push at ``failed``, and
#: ``reset_erp_push_budget`` drops it as every new push starts).
LAST_REFUSAL_CODE_KEY = "erp_last_refusal_code"

#: ``WorkflowInstance.state_data`` key: the person's confirmation that the bill
#: on the shared key is this invoice's, ``{document_id, actor_id, reason,
#: confirmed_at}``. Read by ``services/erp._call_erp`` into
#: ``InvoicePayload.confirmed_bill_id``.
CONFIRMED_BILL_KEY = "erp_confirmed_bill"

#: The push refusal this confirmation answers (``erp_adapters.xero``).
ERP_BILL_KEY_SHARED = "erp_bill_key_shared"

#: The verdict key ``GET /review-actions`` carries, only on a candidate.
CONFIRM_VERDICT_KEY = "erp_bill_confirm_refusal"

#: Audit action for the confirmation (the lookup reuses the orphan lookup's).
ACTION_INVOICE_ERP_BILL_CONFIRMED = "invoice.erp_bill_confirmed"

#: The invoice is not a failed push refused ``erp_bill_key_shared`` by an ERP
#: whose bill key may be shared.
ERP_BILL_CONFIRM_NOT_OFFERED = "erp_bill_confirm_not_offered"
#: The lookup, run again, does not name the bill the person confirmed.
ERP_BILL_CONFIRM_NOT_FOUND = "erp_bill_confirm_not_found"
#: Another invoice already holds or has claimed this bill.
ERP_BILL_CLAIMED = "erp_bill_claimed"

MESSAGES = {
    ERP_BILL_CONFIRM_NOT_OFFERED: (
        "Only an invoice whose ERP push was refused because another invoice shares its "
        "supplier and number can have the bill found there confirmed as its own."
    ),
    ERP_BILL_CONFIRM_NOT_FOUND: (
        "The ERP no longer shows that bill for this invoice's supplier and number. "
        "Look it up again."
    ),
    ERP_BILL_CLAIMED: (
        "Another invoice already holds this bill in the ERP, so it can't be confirmed "
        "as this invoice's."
    ),
}

#: The roles that may retry an ERP push (``POST /retry-erp``), which the
#: confirmation ends in.
CONFIRM_ROLES = ("admin", "ap_manager", "cfo")


def _adapter_shares_key(org_settings: dict | None) -> bool:
    from app.services.erp_bill_void import _configured_adapter_class

    cls = _configured_adapter_class(org_settings)
    return bool(
        cls is not None
        and getattr(cls, "supports_bill_lookup", False) is True
        and getattr(cls, "bill_lookup_key_shared", False) is True
    )


def confirm_candidate(inv: Invoice | None, instance) -> bool:
    """A failed, approved push with no bill of its own whose last refusal was
    ``erp_bill_key_shared``. The instance is read only for a failed invoice."""
    if inv is None or inv.status != InvoiceStatus.failed or inv.erp_document_id:
        return False
    state = getattr(instance, "state_data", None) if instance is not None else None
    return bool(state) and state.get(LAST_REFUSAL_CODE_KEY) == ERP_BILL_KEY_SHARED


def confirm_lookup_refusal(inv: Invoice | None, instance, user, org_settings) -> dict | None:
    """What both endpoints answer before they open a credential: the
    permission, then the invoice and the configured ERP. ``None`` means the
    lookup and the confirmation may run.

    Gated by ``payment.void`` AND a role that may retry the push. ``payment
    .void`` is what the orphan-bill lookup and its void need (decisions §556):
    whoever decides whose a bill in the ERP is decides whether it stays
    payable there. The confirmation then starts the push's retry, so it is no
    wider than ``POST /retry-erp`` either."""
    from app.api.deps import holds_any_permission, holds_any_role
    from app.api.permissions import PERM_PAYMENT_VOID
    from app.api.refusals import coded_refusal, role_not_permitted

    if not holds_any_permission(user, {PERM_PAYMENT_VOID}) or not holds_any_role(
        user, CONFIRM_ROLES
    ):
        return role_not_permitted()
    if (
        not confirm_candidate(inv, instance)
        or not inv.approved_by
        or not _adapter_shares_key(org_settings)
    ):
        return coded_refusal(ERP_BILL_CONFIRM_NOT_OFFERED, MESSAGES[ERP_BILL_CONFIRM_NOT_OFFERED])
    return None


def confirm_verdicts(inv: Invoice | None, instance, user, org_settings) -> dict:
    """``{CONFIRM_VERDICT_KEY: verdict}`` on a candidate, ``{}`` otherwise: an
    absent verdict offers nothing (the §560 pattern)."""
    if not confirm_candidate(inv, instance):
        return {}
    return {CONFIRM_VERDICT_KEY: confirm_lookup_refusal(inv, instance, user, org_settings)}


def confirmed_bill_id(state_data: dict | None) -> str | None:
    """The bill id a person confirmed as this invoice's, or None."""
    record = (state_data or {}).get(CONFIRMED_BILL_KEY)
    if isinstance(record, dict):
        doc = record.get("document_id")
        if isinstance(doc, str) and doc.strip():
            return doc.strip()
    return None


async def payload_for(db: AsyncSession, org, invoice: Invoice):
    """The payload the push would send now, built as the push builds it, so
    the lookup matches the push's own pre-create match. Unlike
    ``erp_bill_void.lookup_payload`` it does not refuse a shared key: naming
    the bill on that key is the point here, and what may be done with it is
    only the confirmation, which :func:`claimed_elsewhere` guards."""
    from app.services.erp import _build_payload, _fetch_line_items, _resolve_erp_refs
    from app.services.erp_bill_void import _LOCAL_ERP
    from app.services.provider_credentials import provider_config

    config = await provider_config(org, "erp") or dict(_LOCAL_ERP)
    line_items = await _fetch_line_items(db, invoice.id)
    refs = await _resolve_erp_refs(db, invoice, line_items, config)
    return _build_payload(invoice, line_items, refs)


async def lock_bill(db: AsyncSession, document_id: str) -> None:
    """Serialize confirmations of one bill for the rest of the transaction, so
    two invoices confirming it at once cannot both pass
    :func:`claimed_elsewhere`."""
    key = f"erp-bill-confirm:{document_id}"
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def claimed_elsewhere(db: AsyncSession, invoice_id: uuid.UUID, document_id: str) -> bool:
    """Does any OTHER invoice in this tenant hold or claim ``document_id``?
    Its bill of record, the bill its own failed push left (as recorded), or a
    confirmation of it. Any status: a done invoice's bill is still its own."""
    from app.models.workflow import WorkflowInstance
    from app.services.erp import ORPHAN_DOCUMENT_ID_KEY

    held = await db.scalar(
        select(Invoice.id)
        .where(Invoice.erp_document_id == document_id, Invoice.id != invoice_id)
        .limit(1)
    )
    if held is not None:
        return True
    state = WorkflowInstance.state_data
    claimed = await db.scalar(
        select(WorkflowInstance.id)
        .where(
            WorkflowInstance.invoice_id != invoice_id,
            or_(
                state[ORPHAN_DOCUMENT_ID_KEY].astext == document_id,
                state[CONFIRMED_BILL_KEY]["document_id"].astext == document_id,
            ),
        )
        .limit(1)
    )
    return claimed is not None


async def record_confirmation(
    db: AsyncSession, *, invoice: Invoice, instance, user, document_id: str, reason: str, found
) -> None:
    """Write the confirmation onto the workflow state and its append-only
    audit row. The caller holds the locks and commits."""
    from app.services.erp_bill_void import _audit_invoice

    now = datetime.now(UTC).isoformat()
    state = dict(instance.state_data or {})
    state[CONFIRMED_BILL_KEY] = {
        "document_id": document_id,
        "actor_id": str(user.id),
        "reason": reason,
        "confirmed_at": now,
    }
    instance.state_data = state
    await _audit_invoice(
        db,
        invoice,
        action=ACTION_INVOICE_ERP_BILL_CONFIRMED,
        actor_id=user.id,
        details={
            "invoice_id": str(invoice.id),
            "erp_document_id": document_id,
            "erp_document_number": getattr(found, "document_number", None),
            "erp_status": found.status.value if found is not None else None,
            "reason": reason,
            "confirmed_at": now,
        },
    )
