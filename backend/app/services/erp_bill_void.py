"""Voiding a flagged bill in the ERP, from the exception that flags it (decisions §539).

A ``duplicate`` or ``fraud_flag`` (``payment_gates.ERP_EXPORT_BLOCKING_EXCEPTION_TYPES``)
refuses the ERP push while it is open (§286), but such a flag can open AFTER the
bill is already in the ERP: a Positive Pay return, a fraud rule re-run on an
edit, a third copy entered later. Our payment run still refuses the invoice;
the ERP's own Bill Pay does not know. This module is the person's way to act on
that from the exception: void the bill there (``ErpAdapter.void_invoice``) and
walk the invoice back to ``approved``, where both gates still hold it until the
flag is decided. Never automatic — the flag may be false.

One rule (issue #575): :func:`void_refusal` is what the queue shows and what
the endpoint raises from before it calls anyone. Gated like a payment void
(``payment.void``), idempotent on a replay, audited append-only.

**Three steps, no row lock across the ERP (decisions §545).** The endpoint
used to hold the invoice, flag and workflow-instance locks across both ERP
calls, so a slow ERP stalled every other writer of that invoice for up to
~105 s. Now:

1. :func:`begin_void`, under the locks: every refusal that needs no ERP call,
   then a durable "void in progress" marker on ``invoices.erp_void_pending``
   (:func:`live_pending`), committed. While it is live a second click is
   refused (``erp_void_in_progress``) and the flag it names cannot be cleared
   (:func:`clear_refusal`), so every payment, card and push gate keeps holding
   the invoice by its existing open-flag predicate.
2. :func:`call_erp`, with no lock and no session: the read, and the void only
   on a positive one (§542).
3. Re-lock: :func:`finish_void` checks the marker is still this attempt's and
   the invoice still holds the same bill, then walks it to ``approved``;
   :func:`abandon_void` clears the marker after a refusal from step 2.

A crash between 1 and 3 leaves a marker no one finishes. It stops being live
after :data:`PENDING_STALE_AFTER_S` (well past the bound on step 2), and the
same endpoint then takes it over: the read finds the bill gone and converges
(§542), or open and voids it — what the person asked for. No sweep: a stale
marker fails closed (the flag stays open, so nothing pays the invoice), and
once stale it blocks nothing, the flag can be decided either way.
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

#: The audit action when this attempt sent a void to the ERP but could not
#: record it here: its marker had been taken over, or the invoice had moved on
#: (decisions §550). The bill may be void in the ERP with nothing else here
#: saying so.
ACTION_EXCEPTION_ERP_BILL_VOID_ORPHANED = "exception.erp_bill_void_orphaned"

#: ``erp_report["kind"]`` of the payment-blocking row an orphaned void opens
#: when the invoice is payable here (decisions §550).
ERP_REPORT_VOID_ORPHANED = "void_orphaned"

#: Bounds, in seconds, on each ERP call of step 2 (decisions §542; since §545
#: no row lock is held across them, and §550 bounds the step as a whole). Each adapter's HTTP
#: client already times out per request (15–30 s); these bound the whole call,
#: which may make several requests (a token refresh, a read before a delete). A
#: read that runs out is ``erp_void_failed`` (nothing was written); a void that
#: runs out is ``erp_void_unconfirmed`` (the ERP may have voided it).
STATUS_READ_TIMEOUT_S = 30
VOID_CALL_TIMEOUT_S = 45

#: Seconds after which a void-in-progress marker no longer counts (decisions
#: §545). Step 2 as a whole is bounded at :data:`CALL_ERP_TIMEOUT_S`, so a
#: marker older than this belongs to an attempt that died between its commit
#: and its finish; the endpoint then takes it over.
PENDING_STALE_AFTER_S = 300

#: The bound on the WHOLE of step 2 (decisions §550): the credential unseal and
#: the adapter's construction as well as the read, the deleted-bill report and
#: the void. The per-call bounds above leave the unseal (a KMS call in a
#: worker thread) unbounded here, and an attempt that outran
#: :data:`PENDING_STALE_AFTER_S` could void a bill in the ERP after its marker
#: stopped holding the flag. Half the staleness window, so step 2 has always
#: given up long before its marker goes stale.
CALL_ERP_TIMEOUT_S = 150
assert CALL_ERP_TIMEOUT_S * 2 <= PENDING_STALE_AFTER_S

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
#: Another attempt to void this bill is calling the ERP right now (§545).
ERP_VOID_IN_PROGRESS = "erp_void_in_progress"

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
    ERP_VOID_IN_PROGRESS: (
        "This bill is being voided in the ERP right now. Wait a few minutes, then "
        "check the flag again."
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
    if live_pending(inv) is not None:
        return coded_refusal(ERP_VOID_IN_PROGRESS, refusal_message(ERP_VOID_IN_PROGRESS))
    return None


def live_pending(inv, *, now: datetime | None = None) -> dict | None:
    """The invoice's void-in-progress marker while it counts, else ``None``.

    A marker counts until :data:`PENDING_STALE_AFTER_S` after it was written; a
    malformed one (no parseable ``started_at``) does not count, since nothing
    this module writes looks like that."""
    marker = getattr(inv, "erp_void_pending", None)
    return marker if _is_live(marker, now=now) else None


def _is_live(marker, *, now: datetime | None = None) -> bool:
    if not isinstance(marker, dict):
        return False
    try:
        started = datetime.fromisoformat(str(marker.get("started_at")))
    except ValueError:
        return False
    if started.tzinfo is None:
        return False
    age = ((now or datetime.now(UTC)) - started).total_seconds()
    return age < PENDING_STALE_AFTER_S


async def clear_refusal(db: AsyncSession, exception: APException) -> str | None:
    """``erp_void_in_progress`` when ``exception`` is the flag a live void of
    its invoice's bill was started from, else ``None`` (decisions §545).

    The flag is what holds the invoice off every payment, card and push gate
    while the void's ERP calls run with no lock held; clearing it then would
    let a run schedule an invoice whose bill is being voided. Race-free with
    :func:`begin_void`: the flag's row is locked here first (the void locks the
    invoice, then this row, and commits its marker before releasing it), then
    the marker is read fresh from the invoice — a column read, not the
    identity map — so either this sees the committed marker or the void sees
    the flag already cleared and refuses ``erp_void_not_flagged``."""
    from sqlalchemy import select

    if not is_void_candidate(exception):
        return None
    await db.execute(select(APException.id).where(APException.id == exception.id).with_for_update())
    marker = (
        await db.execute(select(Invoice.erp_void_pending).where(Invoice.id == exception.invoice_id))
    ).scalar_one_or_none()
    if _is_live(marker) and marker.get("exception_id") == str(exception.id):
        return ERP_VOID_IN_PROGRESS
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


@dataclass(frozen=True)
class VoidAttempt:
    """What step 1 committed, carried to steps 2 and 3."""

    attempt_id: str
    document_id: str
    invoice_status: InvoiceStatus


@dataclass(frozen=True)
class ErpAnswer:
    """What step 2 learned: the bill's read, and whether it was already gone
    (no void sent) or voided now."""

    read: str
    already_gone: bool


def _refused(code: str, status_code: int, **params) -> VoidRefused:
    return VoidRefused(code, status_code, params)


async def begin_void(
    db: AsyncSession,
    *,
    exception: APException,
    invoice: Invoice | None,
    instance,
    user,
    reason: str,
) -> VoidOutcome | VoidAttempt:
    """Step 1, under the caller's locks (invoice, then flag, then instance).

    1. replay: the invoice no longer names a bill and its workflow state records
       a void THIS exception made → the same answer, nothing called;
    2. :func:`void_refusal` (permission, row, bill, a live marker);
    3. no live FeohLedger payment or card (`erp_export_gate.live_claims`);
    4. the marker, written on the invoice. The caller commits it.

    A marker past :data:`PENDING_STALE_AFTER_S` is overwritten: the attempt
    that wrote it died, and this one finishes its work (§545)."""
    from fastapi import status as http_status

    from app.services.erp_export_gate import live_claims

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
        raise _refused(
            code,
            http_status.HTTP_403_FORBIDDEN
            if code == "role_not_permitted"
            else http_status.HTTP_409_CONFLICT,
        )
    assert invoice is not None  # bill_holder() above

    claim = (await live_claims(db, [invoice.id])).get(invoice.id)
    if claim is not None:
        raise _refused(ERP_VOID_CLAIMED, http_status.HTTP_409_CONFLICT, claim=claim)

    attempt = VoidAttempt(
        attempt_id=str(uuid.uuid4()),
        document_id=invoice.erp_document_id,
        invoice_status=invoice.status,
    )
    invoice.erp_void_pending = {
        "attempt_id": attempt.attempt_id,
        "exception_id": str(exception.id),
        "document_id": attempt.document_id,
        "actor_id": str(user.id),
        "reason": reason,
        "started_at": datetime.now(UTC).isoformat(),
    }
    return attempt


@dataclass
class _Phase:
    """How far step 2 got: whether the void call has been started."""

    void_sent: bool = False


async def call_erp(org, attempt: VoidAttempt) -> ErpAnswer:
    """Step 2, bounded as a whole at :data:`CALL_ERP_TIMEOUT_S` (decisions §550).

    Running out before the void call started is ``erp_void_failed`` (nothing
    was written to the ERP); after, ``erp_void_unconfirmed``. The timeout
    cancels the coroutine, so no later step runs: a credential unseal already
    in its worker thread cannot be cancelled and may finish afterwards, but
    nothing awaits its result, so no void follows it."""
    from fastapi import status as http_status

    phase = _Phase()
    try:
        return await asyncio.wait_for(_call_erp(org, attempt, phase), CALL_ERP_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "erp bill void: ERP step timed out (%s the void call)",
            "after" if phase.void_sent else "before",
        )
        if phase.void_sent:
            raise _refused(ERP_VOID_UNCONFIRMED, http_status.HTTP_502_BAD_GATEWAY) from None
        raise _refused(ERP_VOID_FAILED, http_status.HTTP_502_BAD_GATEWAY) from None


async def _call_erp(org, attempt: VoidAttempt, phase: _Phase) -> ErpAnswer:
    """Step 2, with no row lock and no session: open the adapter, read the
    bill, and void it only on a positive read (§542).

    ``open`` / ``draft`` → ``void_invoice`` (False → rejected; raised or timed
    out → ``erp_void_unconfirmed``, since the ERP may have acted);
    ``cancelled``, or ``unknown`` with ``bill_report`` saying ``deleted`` → the
    bill is already gone, so no void is sent (a lost commit converges on
    retry); paid / part-paid / settled with no payment / unreadable → refused.
    Opening the adapter (a KMS unseal) is here too, off the locks."""
    from fastapi import status as http_status

    from app.services.credential_crypto import CredentialCryptoError
    from app.services.erp_adapters import get_erp_adapter
    from app.services.erp_adapters.base import BILL_VOID_DRAFT_ONLY, ErpInvoiceStatus
    from app.services.provider_credentials import provider_config

    try:
        config = await provider_config(org, "erp") or dict(_LOCAL_ERP)
        adapter = get_erp_adapter(config)
    except CredentialCryptoError:
        raise _refused(ERP_VOID_FAILED, http_status.HTTP_502_BAD_GATEWAY) from None
    except Exception:  # noqa: BLE001 — an unknown adapter type cannot void anything
        raise _refused(ERP_VOID_UNSUPPORTED, http_status.HTTP_409_CONFLICT) from None

    void_scope = getattr(adapter, "bill_void_scope", None)
    if void_scope == BILL_VOID_DRAFT_ONLY and attempt.invoice_status == InvoiceStatus.posted_in_erp:
        raise _refused(ERP_VOID_DRAFT_ONLY, http_status.HTTP_409_CONFLICT)

    document_id = attempt.document_id
    try:
        read = await asyncio.wait_for(
            adapter.get_invoice_status(document_id), STATUS_READ_TIMEOUT_S
        )
    except NotImplementedError:
        read = None
    except Exception as exc_:  # noqa: BLE001 — never void a bill whose state is unknown
        logger.warning("erp bill void: status read failed (%s)", exc_.__class__.__name__)
        raise _refused(ERP_VOID_FAILED, http_status.HTTP_502_BAD_GATEWAY) from None
    if read in (ErpInvoiceStatus.paid, ErpInvoiceStatus.partially_paid):
        raise _refused(ERP_VOID_BILL_PAID, http_status.HTTP_409_CONFLICT)
    if read == ErpInvoiceStatus.settled_no_payment:
        raise _refused(ERP_VOID_BILL_SETTLED, http_status.HTTP_409_CONFLICT)
    already_gone = read == ErpInvoiceStatus.cancelled
    if read in (None, ErpInvoiceStatus.unknown):
        already_gone = await _positively_gone(adapter, document_id)
        if not already_gone:
            raise _refused(ERP_VOID_FAILED, http_status.HTTP_502_BAD_GATEWAY)

    if not already_gone:
        if void_scope == BILL_VOID_DRAFT_ONLY and read != ErpInvoiceStatus.draft:
            raise _refused(ERP_VOID_DRAFT_ONLY, http_status.HTTP_409_CONFLICT)
        phase.void_sent = True
        try:
            voided = await asyncio.wait_for(adapter.void_invoice(document_id), VOID_CALL_TIMEOUT_S)
        except NotImplementedError:
            raise _refused(ERP_VOID_UNSUPPORTED, http_status.HTTP_409_CONFLICT) from None
        except Exception as exc_:  # noqa: BLE001 — the ERP may have voided it
            logger.warning("erp bill void: void call unconfirmed (%s)", exc_.__class__.__name__)
            raise _refused(ERP_VOID_UNCONFIRMED, http_status.HTTP_502_BAD_GATEWAY) from None
        if not voided:
            raise _refused(ERP_VOID_REJECTED, http_status.HTTP_409_CONFLICT)

    # A bill found already voided reads `cancelled` whichever way the ERP said
    # so (a status read, or a report of it deleted).
    return ErpAnswer(read="cancelled" if already_gone else read.value, already_gone=already_gone)


def _ours(invoice: Invoice | None, attempt: VoidAttempt) -> bool:
    marker = getattr(invoice, "erp_void_pending", None)
    return isinstance(marker, dict) and marker.get("attempt_id") == attempt.attempt_id


def _still_holds(invoice: Invoice, attempt: VoidAttempt) -> bool:
    """Does the invoice still hold the bill this attempt set out to void, at a
    status a void may finish from?"""
    return invoice.erp_document_id == attempt.document_id and invoice.status in VOIDABLE_STATUSES


def lost_its_invoice(invoice: Invoice | None, attempt: VoidAttempt) -> bool:
    """Can this attempt no longer record its void here: its marker taken over,
    or the invoice moved on from the bill?"""
    return invoice is None or not _ours(invoice, attempt) or not _still_holds(invoice, attempt)


def configured_erp_type(org_settings: dict | None) -> str | None:
    """The adapter key the org's ERP configuration resolves to, for naming the
    ERP in a finding (the local default when none is configured)."""
    from app.services.erp_adapters.dispatcher import resolve_adapter_key

    erp = (org_settings or {}).get("erp") if isinstance(org_settings, dict) else None
    return resolve_adapter_key(erp if isinstance(erp, dict) and erp else _LOCAL_ERP)


async def record_orphaned_void(
    db: AsyncSession,
    *,
    attempt: VoidAttempt,
    exception: APException,
    invoice: Invoice | None,
    user,
    void_result: str,
    erp_type: str | None,
) -> None:
    """This attempt sent the void to the ERP (``void_result`` ``voided``, or
    ``unconfirmed`` when the ERP may have acted) but cannot record it here
    (decisions §550): its marker went stale and was taken over, or the invoice
    moved on (a person dismissed the flag once the marker was stale and a run
    scheduled it). Nothing on the invoice is changed — what happened to it
    since is someone's decision — but the void must not go unrecorded:

    * an append-only ``exception.erp_bill_void_orphaned`` row on the flag,
      naming the bill, the actor, the attempt and the invoice's status now;
    * when the invoice no longer holds the bill and is payable here
      (``PAYABLE_STATUSES``), a payment-blocking ``payment_reconciliation``
      row (kind ``void_orphaned``, deduped by kind), since a payment against a
      bill void in the ERP needs a person. An invoice still holding the same
      bill, its marker taken over by a newer attempt, gets none: that attempt
      reads the bill gone and converges, and the open flag holds it meanwhile."""
    from app.models.invoice_status import PAYABLE_STATUSES
    from app.services.erp_status_sync import (
        PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
        erp_finding,
        open_erp_reconciliation_exception,
    )
    from app.services.exception_lifecycle import _write

    status_now = invoice.status.value if invoice is not None else None
    logger.warning(
        "erp bill void: attempt %s %s bill on invoice %s but could not record it",
        attempt.attempt_id,
        void_result,
        getattr(invoice, "id", None),
    )
    await _write(
        db,
        exception=exception,
        invoice=invoice,
        action=ACTION_EXCEPTION_ERP_BILL_VOID_ORPHANED,
        actor_id=user.id,
        details={
            "exception_id": str(exception.id),
            "invoice_id": str(exception.invoice_id),
            "erp_document_id": attempt.document_id,
            "attempt_id": attempt.attempt_id,
            "actor_id": str(user.id),
            "void_result": void_result,
            "invoice_status": status_now,
            "marker_taken_over": invoice is not None and not _ours(invoice, attempt),
        },
    )
    if (
        invoice is not None
        and not _still_holds(invoice, attempt)
        and invoice.status in PAYABLE_STATUSES
    ):
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=exception.organization_id,
            finding=erp_finding("erp_voided_after_post", erp_type),
            exception_type=PAYMENT_RECONCILIATION_EXCEPTION_TYPE,
            erp_report={
                "kind": ERP_REPORT_VOID_ORPHANED,
                "erp_type": erp_type,
                "erp_document_id": attempt.document_id,
                "attempt_id": attempt.attempt_id,
                "void_result": void_result,
                "invoice_status": status_now,
                "exception_id": str(exception.id),
            },
        )


def abandon_void(invoice: Invoice | None, attempt: VoidAttempt) -> None:
    """Step 3 after a refusal from step 2, under the re-taken invoice lock:
    clear the marker if it is still this attempt's. Nothing else was written;
    the flag stays open, and a retry starts over (an unconfirmed void then
    converges, §542)."""
    if invoice is not None and _ours(invoice, attempt):
        invoice.erp_void_pending = None


async def finish_void(
    db: AsyncSession,
    *,
    attempt: VoidAttempt,
    answer: ErpAnswer,
    exception: APException,
    invoice: Invoice | None,
    instance,
    user,
    reason: str,
    erp_type: str | None = None,
) -> VoidOutcome:
    """Step 3 after the ERP voided the bill (or it was already gone), under the
    re-taken locks, in the caller's transaction.

    The marker must still be this attempt's — one taken over after going stale
    belongs to an attempt that will converge on its own read — and the invoice
    must still hold the same bill at a voidable status; otherwise nothing here
    is written (``erp_void_in_progress`` / ``erp_void_no_bill``, the marker
    cleared in the second case so it cannot hold the flag) but, when this
    attempt sent the void, the orphan record (:func:`record_orphaned_void`).

    Then: the document id, its deep link and the push's ``erp_reference`` are
    cleared (so `review.recall_refusal` no longer finds a bill — any other
    possibly-posted record stays and keeps refusing the recall), the void is
    recorded on the workflow state, the invoice transitions ``→ approved``
    (`workflow_engine.ERP_VOID_ACTION`, its own audit row), the exception gets
    an append-only ``exception.erp_bill_voided`` row and the marker is cleared.
    The exception itself stays open: the flag is still a person's to decide."""
    from fastapi import status as http_status

    from app.services.exception_lifecycle import _write
    from app.services.workflow_engine import ERP_VOID_ACTION, transition_invoice

    if invoice is None or not _ours(invoice, attempt):
        if not answer.already_gone:
            await record_orphaned_void(
                db,
                attempt=attempt,
                exception=exception,
                invoice=invoice,
                user=user,
                void_result="voided",
                erp_type=erp_type,
            )
        raise _refused(ERP_VOID_IN_PROGRESS, http_status.HTTP_409_CONFLICT)
    document_id = attempt.document_id
    if not _still_holds(invoice, attempt):
        logger.warning(
            "erp bill void: invoice %s changed while its bill was voided; not finished",
            invoice.id,
        )
        if not answer.already_gone:
            await record_orphaned_void(
                db,
                attempt=attempt,
                exception=exception,
                invoice=invoice,
                user=user,
                void_result="voided",
                erp_type=erp_type,
            )
        invoice.erp_void_pending = None
        raise _refused(ERP_VOID_NO_BILL, http_status.HTTP_409_CONFLICT)

    now = datetime.now(UTC)
    previous_status = invoice.status.value
    record = {
        "document_id": document_id,
        "exception_id": str(exception.id),
        "voided_at": now.isoformat(),
        "actor_id": str(user.id),
        "already_gone": answer.already_gone,
        "attempt_id": attempt.attempt_id,
    }
    invoice.erp_document_id = None
    invoice.erp_document_url = None
    invoice.erp_void_pending = None
    if instance is not None:
        state = dict(instance.state_data or {})
        state.pop("erp_reference", None)
        state[ERP_BILL_VOIDED_KEY] = record
        instance.state_data = state

    details = {
        "erp_document_id": document_id,
        "exception_id": str(exception.id),
        "exception_type": exception.exception_type,
        "reason": reason,
        "erp_status_read": answer.read,
        "already_gone": answer.already_gone,
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
            "already_gone": answer.already_gone,
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
