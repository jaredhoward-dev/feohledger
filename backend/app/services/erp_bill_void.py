"""Voiding a flagged bill in the ERP, from the exception that flags it (decisions §539).

A ``duplicate`` or ``fraud_flag`` (``payment_gates.ERP_EXPORT_BLOCKING_EXCEPTION_TYPES``)
refuses the ERP push while it is open (§286), but such a flag can open AFTER the
bill is already in the ERP: a Positive Pay return, a fraud rule re-run on an
edit, a third copy entered later. Our payment run still refuses the invoice;
the ERP's own Bill Pay does not know. This module is the person's way to act on
that from the exception: void the bill there (``ErpAdapter.void_invoice``) and
walk the invoice back to ``approved``, where both gates still hold it until the
flag is decided. Never automatic — the flag may be false.

Two functions, one rule (issue #575): :func:`void_refusal` is what the queue
shows and what the endpoint raises from before it calls anyone;
:func:`void_flagged_erp_bill` does it. Gated like a payment void
(``payment.void``), on a locked invoice, idempotent on a replay, audited
append-only.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.services.exception_lifecycle import ACTIONABLE_STATUSES
from app.services.payment_gates import ERP_EXPORT_BLOCKING_EXCEPTION_TYPES

logger = logging.getLogger(__name__)

#: The statuses an invoice holds a confirmed bill in the ERP in and nothing
#: else holds it: from `payment_scheduled` on a FeohLedger payment does, and the
#: live-claim gate answers for it. `failed` with a possibly-posted bill has no
#: document id to void (the orphan lookup is its own follow-up).
VOIDABLE_STATUSES = frozenset({InvoiceStatus.sent_to_erp, InvoiceStatus.posted_in_erp})

#: `WorkflowInstance.state_data` key recording the void this module made:
#: ``{"document_id", "exception_id", "voided_at", "actor_id"}``. A replay finds
#: it and answers with it instead of calling the ERP a second time.
ERP_BILL_VOIDED_KEY = "erp_bill_voided"

#: The audit action on the exception row; the invoice's own transition row is
#: ``workflow_engine.ERP_VOID_ACTION``.
ACTION_EXCEPTION_ERP_BILL_VOIDED = "exception.erp_bill_voided"

#: Bounds, in seconds, on the two ERP calls made while the invoice, its flag and
#: its workflow instance are row-locked (decisions §542). Each adapter's HTTP
#: client already times out per request (15–30 s); these bound the whole call,
#: which may make several requests (a token refresh, a read before a delete). A
#: read that runs out is ``erp_void_failed`` (nothing was written); a void that
#: runs out is ``erp_void_unconfirmed`` (the ERP may have voided it).
STATUS_READ_TIMEOUT_S = 30
VOID_CALL_TIMEOUT_S = 45

# ---------- the coded refusals (`api/refusals.coded_refusal`) ----------

#: The row is not an open/escalated `duplicate` / `fraud_flag` on an invoice.
ERP_VOID_NOT_FLAGGED = "erp_void_not_flagged"
#: The invoice holds no confirmed bill FeohLedger can name.
ERP_VOID_NO_BILL = "erp_void_no_bill"
#: A FeohLedger payment or virtual card is already paying it (`params.claim`).
ERP_VOID_CLAIMED = "erp_void_claimed"
#: The ERP reads the bill as paid or part-paid: voiding it would not undo that.
ERP_VOID_BILL_PAID = "erp_void_bill_paid"
#: The configured ERP adapter cannot void a bill.
ERP_VOID_UNSUPPORTED = "erp_void_unsupported"
#: The ERP answered that it did not void the bill.
ERP_VOID_REJECTED = "erp_void_rejected"
#: Before any write to the ERP, the call failed (network, credentials) or the
#: read did not say where the bill stands: nothing was changed.
ERP_VOID_FAILED = "erp_void_failed"
#: The void call itself failed or timed out: the ERP may have voided the bill.
#: A retry reads the bill first and, if it is gone, finishes the job (§542).
ERP_VOID_UNCONFIRMED = "erp_void_unconfirmed"
#: The ERP reads the bill as settled with no payment (a credit, a write-off).
ERP_VOID_BILL_SETTLED = "erp_void_bill_settled"
#: The connected ERP can void only a draft / pending-approval bill, and this one
#: is past that (`ErpAdapter.bill_void_scope`, §542).
ERP_VOID_DRAFT_ONLY = "erp_void_draft_only"

_MESSAGES = {
    ERP_VOID_NOT_FLAGGED: (
        "Only an open duplicate or fraud flag on an invoice can void its bill in the ERP."
    ),
    ERP_VOID_NO_BILL: "FeohLedger has no bill in the ERP for this invoice to void.",
    ERP_VOID_CLAIMED: (
        "FeohLedger is already paying this invoice. Void that payment or card first."
    ),
    ERP_VOID_BILL_PAID: (
        "The ERP shows this bill as paid or partly paid, so voiding it would not undo "
        "that. Reverse the payment in the ERP first."
    ),
    ERP_VOID_UNSUPPORTED: "The connected ERP can't void a bill from FeohLedger. Void it there.",
    ERP_VOID_REJECTED: "The ERP did not void the bill. Check it in the ERP.",
    ERP_VOID_FAILED: (
        "The ERP could not be reached, or did not say where the bill stands. Nothing "
        "was changed; try again."
    ),
    ERP_VOID_UNCONFIRMED: (
        "The ERP did not confirm the void. Check the bill in the ERP before retrying."
    ),
    ERP_VOID_BILL_SETTLED: (
        "The ERP shows nothing owing on this bill but no payment against it (a credit "
        "or a write-off). Check it in the ERP."
    ),
    ERP_VOID_DRAFT_ONLY: (
        "The connected ERP can void a bill from FeohLedger only while it is a draft or "
        "awaiting approval there. Reverse this one in the ERP."
    ),
}


def refusal_message(code: str) -> str:
    return _MESSAGES[code]


@dataclass(frozen=True)
class VoidRefused(Exception):  # noqa: N818 — a refusal, not an error
    """A refusal :func:`void_flagged_erp_bill` raised before anything was written."""

    code: str
    status_code: int
    params: dict

    def detail(self) -> dict:
        from app.api.refusals import coded_refusal

        return coded_refusal(self.code, refusal_message(self.code), **self.params)


def is_void_candidate(exc: APException) -> bool:
    """Is this the kind of row the void is offered from at all? (The queue adds
    the verdict only on these, so other rows carry nothing.)"""
    return exc.exception_type in ERP_EXPORT_BLOCKING_EXCEPTION_TYPES and exc.invoice_id is not None


def bill_holder(inv: Invoice | None) -> bool:
    """Does ``inv`` hold a confirmed bill in the ERP this module may void?"""
    return bool(inv is not None and inv.erp_document_id and inv.status in VOIDABLE_STATUSES)


def bill_void_scope(org_settings: dict | None) -> str | None:
    """The configured ERP adapter's :attr:`ErpAdapter.bill_void_scope`, read
    from its class: no credential is opened and nothing is called. ``None`` for
    an adapter we do not have (the endpoint refuses it as unsupported)."""
    from app.services.erp_adapters.dispatcher import adapter_class, resolve_adapter_key

    erp = (org_settings or {}).get("erp") if isinstance(org_settings, dict) else None
    config = erp if isinstance(erp, dict) and erp else _LOCAL_ERP
    cls = adapter_class(resolve_adapter_key(config))
    return getattr(cls, "bill_void_scope", None) if cls is not None else None


#: The local-first default the void (and every ERP writer) uses when the org
#: configures no ERP.
_LOCAL_ERP = {"type": "mock", "integration_method": "direct"}


def void_refusal(
    exc: APException, inv: Invoice | None, user, *, void_scope: str | None = None
) -> dict | None:
    """What ``POST /api/exceptions/{id}/void-erp-bill`` would answer ``user``
    before it reads the claim gates or calls the ERP: the permission, the row,
    the bill, and — given the adapter's ``void_scope`` — a bill the ERP's API
    cannot void (a draft-only ERP's bill at ``posted_in_erp``, which the push
    confirmed past draft). ``None`` offers the button. The live-claim, paid-in-
    ERP and other adapter refusals need a read or a network call, so they are
    the endpoint's alone and come back coded from it."""
    from app.api.deps import holds_any_permission
    from app.api.permissions import PERM_PAYMENT_VOID
    from app.api.refusals import coded_refusal, role_not_permitted

    if not holds_any_permission(user, {PERM_PAYMENT_VOID}):
        return role_not_permitted()
    if not is_void_candidate(exc) or exc.status not in ACTIONABLE_STATUSES:
        return coded_refusal(ERP_VOID_NOT_FLAGGED, refusal_message(ERP_VOID_NOT_FLAGGED))
    if not bill_holder(inv):
        return coded_refusal(ERP_VOID_NO_BILL, refusal_message(ERP_VOID_NO_BILL))
    if _draft_only_refuses(void_scope, inv):
        return coded_refusal(ERP_VOID_DRAFT_ONLY, refusal_message(ERP_VOID_DRAFT_ONLY))
    return None


def _draft_only_refuses(void_scope: str | None, inv) -> bool:
    from app.services.erp_adapters.base import BILL_VOID_DRAFT_ONLY

    return void_scope == BILL_VOID_DRAFT_ONLY and inv.status == InvoiceStatus.posted_in_erp


async def _positively_gone(adapter, document_id: str) -> bool:
    """Does the ERP POSITIVELY say the bill is gone, when its status read could
    not say? ``bill_report``'s ``deleted`` is that answer for every adapter that
    has it (QuickBooks reads a deleted bill as unreadable, ``unknown``, in
    ``get_invoice_status``); a failure, or the base class's ``unknown``, is not."""
    from app.services.erp_adapters.base import ErpBillState

    try:
        report = await asyncio.wait_for(adapter.bill_report(document_id), STATUS_READ_TIMEOUT_S)
    except Exception as exc_:  # noqa: BLE001 — unreadable is never gone
        logger.warning("erp bill void: bill report failed (%s)", exc_.__class__.__name__)
        return False
    return getattr(report, "state", None) == ErpBillState.deleted


@dataclass(frozen=True)
class VoidOutcome:
    document_id: str
    replayed: bool
    status: str


async def void_flagged_erp_bill(
    db: AsyncSession,
    *,
    org,
    exception: APException,
    invoice: Invoice | None,
    instance,
    user,
    reason: str,
) -> VoidOutcome:
    """Void ``invoice``'s bill in the ERP and walk it back to ``approved``.

    The caller holds ``invoice`` (then ``exception``) locked FOR UPDATE, so two
    clicks serialize: the second sees the first's result. Order:

    1. replay: the invoice no longer names a bill and its workflow state records
       a void THIS exception made → the same answer, nothing called (idempotent);
    2. :func:`void_refusal` (permission, row, bill);
    3. no live FeohLedger payment or card (`erp_export_gate.live_claims`);
    4. the adapter: unknown/unsupported → refused; a draft-only ERP's bill past
       draft → refused; then the read, and only a positive one goes on (§542):
       ``open`` / ``draft`` → ``void_invoice`` (False → rejected; raised or
       timed out → ``erp_void_unconfirmed``, since the ERP may have acted);
       ``cancelled``, or ``unknown`` with ``bill_report`` saying ``deleted`` →
       the bill is already gone, so no void is sent and the rest below runs as
       if it had been (a lost commit converges on retry); paid / part-paid /
       settled with no payment / unreadable → refused. Every refusal comes
       before any write here.

    On success, in the caller's transaction: the document id, its deep link and
    the push's ``erp_reference`` are cleared (so `review.recall_refusal` no
    longer finds a bill — any other possibly-posted record stays and keeps
    refusing the recall), the void is recorded on the workflow state, the
    invoice transitions ``→ approved`` (`workflow_engine.ERP_VOID_ACTION`, its
    own audit row) and the exception gets an append-only
    ``exception.erp_bill_voided`` row. The exception itself stays open: the
    flag is still a person's to decide.
    """
    from fastapi import status as http_status

    from app.services.credential_crypto import CredentialCryptoError
    from app.services.erp_adapters import get_erp_adapter
    from app.services.erp_adapters.base import BILL_VOID_DRAFT_ONLY, ErpInvoiceStatus
    from app.services.erp_export_gate import live_claims
    from app.services.exception_lifecycle import _write
    from app.services.provider_credentials import provider_config
    from app.services.workflow_engine import ERP_VOID_ACTION, transition_invoice

    state = dict((getattr(instance, "state_data", None) or {}) if instance is not None else {})
    previous = state.get(ERP_BILL_VOIDED_KEY)
    if (
        invoice is not None
        and not invoice.erp_document_id
        and isinstance(previous, dict)
        and previous.get("exception_id") == str(exception.id)
    ):
        return VoidOutcome(
            document_id=str(previous.get("document_id") or ""),
            replayed=True,
            status=invoice.status.value,
        )

    verdict = void_refusal(exception, invoice, user)
    if verdict is not None:
        code = verdict["code"]
        raise VoidRefused(
            code,
            http_status.HTTP_403_FORBIDDEN
            if code == "role_not_permitted"
            else http_status.HTTP_409_CONFLICT,
            {},
        )
    assert invoice is not None  # bill_holder() above

    claim = (await live_claims(db, [invoice.id])).get(invoice.id)
    if claim is not None:
        raise VoidRefused(ERP_VOID_CLAIMED, http_status.HTTP_409_CONFLICT, {"claim": claim})

    try:
        config = await provider_config(org, "erp") or dict(_LOCAL_ERP)
        adapter = get_erp_adapter(config)
    except CredentialCryptoError:
        raise VoidRefused(ERP_VOID_FAILED, http_status.HTTP_502_BAD_GATEWAY, {}) from None
    except Exception:  # noqa: BLE001 — an unknown adapter type cannot void anything
        raise VoidRefused(ERP_VOID_UNSUPPORTED, http_status.HTTP_409_CONFLICT, {}) from None

    void_scope = getattr(adapter, "bill_void_scope", None)
    if _draft_only_refuses(void_scope, invoice):
        raise VoidRefused(ERP_VOID_DRAFT_ONLY, http_status.HTTP_409_CONFLICT, {})

    # Void only on a positive read (decisions §542): `open` / `draft` proceed;
    # `cancelled`, or a bill the ERP positively reports deleted, is a void that
    # already happened (ours, whose commit was lost, or someone's in the ERP)
    # and converges without a second call; anything else is refused.
    document_id = invoice.erp_document_id
    try:
        read = await asyncio.wait_for(
            adapter.get_invoice_status(document_id), STATUS_READ_TIMEOUT_S
        )
    except NotImplementedError:
        read = None
    except Exception as exc_:  # noqa: BLE001 — never void a bill whose state is unknown
        logger.warning("erp bill void: status read failed (%s)", exc_.__class__.__name__)
        raise VoidRefused(ERP_VOID_FAILED, http_status.HTTP_502_BAD_GATEWAY, {}) from None
    if read in (ErpInvoiceStatus.paid, ErpInvoiceStatus.partially_paid):
        raise VoidRefused(ERP_VOID_BILL_PAID, http_status.HTTP_409_CONFLICT, {})
    if read == ErpInvoiceStatus.settled_no_payment:
        raise VoidRefused(ERP_VOID_BILL_SETTLED, http_status.HTTP_409_CONFLICT, {})
    already_gone = read == ErpInvoiceStatus.cancelled
    if read in (None, ErpInvoiceStatus.unknown):
        already_gone = await _positively_gone(adapter, document_id)
        if not already_gone:
            raise VoidRefused(ERP_VOID_FAILED, http_status.HTTP_502_BAD_GATEWAY, {})

    if not already_gone:
        if void_scope == BILL_VOID_DRAFT_ONLY and read != ErpInvoiceStatus.draft:
            raise VoidRefused(ERP_VOID_DRAFT_ONLY, http_status.HTTP_409_CONFLICT, {})
        try:
            voided = await asyncio.wait_for(adapter.void_invoice(document_id), VOID_CALL_TIMEOUT_S)
        except NotImplementedError:
            raise VoidRefused(ERP_VOID_UNSUPPORTED, http_status.HTTP_409_CONFLICT, {}) from None
        except Exception as exc_:  # noqa: BLE001 — the ERP may have voided it
            logger.warning("erp bill void: void call unconfirmed (%s)", exc_.__class__.__name__)
            raise VoidRefused(ERP_VOID_UNCONFIRMED, http_status.HTTP_502_BAD_GATEWAY, {}) from None
        if not voided:
            raise VoidRefused(ERP_VOID_REJECTED, http_status.HTTP_409_CONFLICT, {})

    now = datetime.now(UTC)
    previous_status = invoice.status.value
    record = {
        "document_id": document_id,
        "exception_id": str(exception.id),
        "voided_at": now.isoformat(),
        "actor_id": str(user.id),
        "already_gone": already_gone,
    }
    invoice.erp_document_id = None
    invoice.erp_document_url = None
    if instance is not None:
        state.pop("erp_reference", None)
        state[ERP_BILL_VOIDED_KEY] = record
        instance.state_data = state

    details = {
        "erp_document_id": document_id,
        "exception_id": str(exception.id),
        "exception_type": exception.exception_type,
        "reason": reason,
        # A bill found already voided reads `cancelled` whichever way the ERP
        # said so (a status read, or a report of it deleted).
        "erp_status_read": "cancelled" if already_gone else read.value,
        "already_gone": already_gone,
    }
    await transition_invoice(
        db,
        invoice,
        InvoiceStatus.approved,
        actor_id=user.id,
        action_name=ERP_VOID_ACTION,
        details=details,
    )
    await _write(
        db,
        exception=exception,
        invoice=invoice,
        action=ACTION_EXCEPTION_ERP_BILL_VOIDED,
        actor_id=user.id,
        details={
            "exception_id": str(exception.id),
            "invoice_id": str(invoice.id),
            "erp_document_id": document_id,
            "previous_invoice_status": previous_status,
            "already_gone": already_gone,
            "reason": reason,
            "voided_at": now.isoformat(),
        },
    )
    return VoidOutcome(document_id=document_id, replayed=False, status=invoice.status.value)


async def notify_flag_on_posted_bill(
    db: AsyncSession, *, exception: APException, invoice: Invoice | None
) -> None:
    """Tell the people who may void it that a `duplicate` / `fraud_flag` opened
    on an invoice whose bill is already in the ERP, where it is payable past our
    gate. Called from ``exception_service.create_exception``; best-effort,
    never raises. A caller that passed only the invoice id (Positive Pay) has it
    loaded here.

    Recipients are the org's active users whose EFFECTIVE permissions include
    ``payment.void`` (a custom role granting it included, an AP manager without
    it not): the message tells its reader to void the bill, so it goes to the
    people the endpoint lets do that (decisions §542). The body runs in a
    savepoint, so a failed statement in it cannot abort the caller's
    transaction, which holds the flag being raised."""
    if not is_void_candidate(exception):
        return
    try:
        async with db.begin_nested():
            if invoice is None:
                invoice = await db.get(Invoice, exception.invoice_id)
            if not bill_holder(invoice):
                return
            from app.api.permissions import PERM_PAYMENT_VOID
            from app.models.notification import EVENT_ERP_BILL_FLAGGED
            from app.services.notification_dispatch import (
                notify_event,
                resolve_permission_user_ids,
            )
            from app.services.notification_templates import render_erp_bill_flagged

            recipients = await resolve_permission_user_ids(
                exception.organization_id, PERM_PAYMENT_VOID
            )
            if not recipients:
                return
            await notify_event(
                db,
                correlation_id=invoice.correlation_id or uuid.uuid4(),
                organization_id=exception.organization_id,
                event_type=EVENT_ERP_BILL_FLAGGED,
                entity_id=exception.id,
                recipient_user_ids=recipients,
                rendered=render_erp_bill_flagged(
                    exception_type=exception.exception_type,
                    invoice_number=invoice.invoice_number,
                ),
                entity_type="exception",
            )
    except Exception:  # noqa: BLE001 — a notice must never break raising the flag
        logger.exception("erp bill flagged notice failed for exception=%s", exception.id)
