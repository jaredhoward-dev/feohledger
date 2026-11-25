"""Idempotent PEPPOL outbound send service.

Orchestrates one transmission of an invoice over the PEPPOL network:

    map → assert_valid(check_tax) → generate_ubl → resolve_participant →
    INSERT PeppolTransmission('sending') + audit ``invoice.peppol_send_started``
    → COMMIT (the reservation) → adapter.send → update row → dispatch_audit →
    commit

Reserve → commit → call → record (decisions §578). The route holds the
invoice's row lock (`FOR UPDATE`) while it checks the invoice and this module
reserves the slot; the reservation's commit releases that lock BEFORE the
Access Point is called, so payment sync, run building and approvals never wait
on a network timeout. The approval recall locks the same row and refuses on any
transmission not ``failed`` — ``sending`` included, its outcome being unknown —
so a recall either commits first (and the send, re-reading the status under
the lock, refuses) or sees the committed reservation (and refuses). The SMP
lookup (``resolve_participant``) is network too: the route runs it before it
takes the lock (:func:`resolve_receiver`) and passes the adapter in.

A ``send`` that raises (rather than returning a failed result, which every
adapter does for transport errors) leaves the reservation ``sending``: whether
the document left is unknown, and an unknown send counts as sent, exactly as
an unknown ERP push does. So does a process that dies between the two commits.
Such a row is settled by :func:`settle_stale_sending` (the operator script
``scripts/settle_peppol_sending.py``), which asks the Access Point and never
marks a row ``failed`` without its answer (decisions §580).

Reuses the shipped ``e_invoice`` package wholesale (no UBL duplication) and the
``peppol_adapters`` family. Idempotency is enforced at the DATA layer by the
partial unique index ``uq_peppol_one_live_per_invoice_direction`` — the row is
inserted in ``sending`` state and flushed BEFORE the (slow, networked) transmit,
so two concurrent sends can never both reach the gateway. A handler-level
short-circuit returns an existing live row without a second adapter call or a
duplicate audit row.

PII invariant: the receiver participant value (a supplier tax/org id) lives on
the row and inside the UBL payload legitimately, but is NEVER logged or placed
in an audit ``details`` payload — only the scheme, message id, doc type, and
provider are recorded. :class:`PeppolSendError` and ``EInvoiceValidationError``
carry PII-free codes only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice, InvoiceLineItem
from app.models.peppol_transmission import PeppolTransmission
from app.services.audit_dispatch import dispatch_audit
from app.services.e_invoice import (
    BuyerIdentity,
    assert_bis3_conformant,
    assert_valid,
    generate_ubl,
    invoice_to_einvoice_document,
)
from app.services.peppol_adapters import (
    PEPPOL_BIS_BILLING_DOCTYPE,
    PEPPOL_BIS_BILLING_PROCESSID,
    ParticipantId,
    PeppolAdapter,
    PeppolSendError,
    TransmissionLookup,
    TransmissionRequest,
    UnknownPeppolProviderError,
    get_peppol_adapter,
)

logger = logging.getLogger(__name__)

_LIVE_STATUSES = ("sending", "sent", "delivered")
_DIRECTION_OUTBOUND = "outbound"


async def _select_live_outbound(
    db: AsyncSession, invoice_id: uuid.UUID
) -> PeppolTransmission | None:
    """Return the existing non-failed outbound transmission for the invoice, if any."""
    return (
        await db.execute(
            select(PeppolTransmission).where(
                PeppolTransmission.invoice_id == invoice_id,
                PeppolTransmission.direction == _DIRECTION_OUTBOUND,
                PeppolTransmission.status.in_(_LIVE_STATUSES),
            )
        )
    ).scalar_one_or_none()


async def live_outbound_transmission(
    db: AsyncSession, invoice_id: uuid.UUID
) -> PeppolTransmission | None:
    """The invoice's live (not ``failed``) outbound transmission, if any — the
    route's unlocked idempotency fast path, ahead of the SMP lookup."""
    return await _select_live_outbound(db, invoice_id)


async def resolve_receiver(peppol_config: dict | None, receiver_id: ParticipantId) -> PeppolAdapter:
    """Resolve the configured adapter and check the receiver over SMP/SML.

    Raises :class:`PeppolSendError` (PII-free code) before anything is
    persisted. Networked, so the route calls it BEFORE it locks the invoice.

    A provider we have no adapter for is refused rather than resolved to
    ``mock`` (`decisions.md` §29). The mock's ``send`` reports success with a
    synthetic message id and no network involved, which would be written onto
    the row as ``sent`` + a ``message_id`` and recorded as
    ``invoice.peppol_sent`` — a legally-significant document reported as
    transmitted to a supplier that never received it, with the
    live-transmission slot occupied so the honest resend came back
    ``already_sent``. Refusing here means no row exists at all. The reason code
    names the condition, never the admin's raw settings value.
    """
    try:
        adapter = get_peppol_adapter(peppol_config)
    except UnknownPeppolProviderError as exc:
        logger.warning(
            "[peppol] provider %r has no registered adapter — refusing to transmit",
            exc.provider,
        )
        raise PeppolSendError("peppol_provider_not_configured") from None
    capability = await adapter.resolve_participant(receiver_id)
    if not capability.registered:
        raise PeppolSendError(capability.unregistered_reason or "receiver_not_registered")
    # The receiver must actually accept the BIS Billing 3.0 invoice doc type —
    # the SMP step exists to catch this before we commit a live row + emit. Only
    # enforce when the AP reported a doc-type set (an empty tuple means the
    # mock/gateway didn't enumerate, so we don't false-reject).
    if (
        capability.supported_doc_types
        and PEPPOL_BIS_BILLING_DOCTYPE not in capability.supported_doc_types
    ):
        raise PeppolSendError("receiver_doctype_unsupported")
    return adapter


async def send_invoice_over_peppol(
    db: AsyncSession,
    *,
    invoice: Invoice,
    line_items: list[InvoiceLineItem],
    buyer: BuyerIdentity,
    sender_id: ParticipantId,
    receiver_id: ParticipantId,
    organization_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    actor_id: uuid.UUID,
    peppol_config: dict | None,
    adapter: PeppolAdapter | None = None,
) -> tuple[PeppolTransmission, bool]:
    """Transmit ``invoice`` over PEPPOL. Idempotent.

    Returns ``(transmission, already_sent)``. ``already_sent`` is True when the
    idempotency short-circuit (or the IntegrityError race path) returned a
    pre-existing live transmission without re-transmitting.

    Raises :class:`app.services.e_invoice.EInvoiceValidationError` (tax-invalid,
    422 at the route) or :class:`PeppolSendError` (e.g.
    ``receiver_not_registered``, 422 at the route) before any row is persisted.

    ``adapter``: the one :func:`resolve_receiver` returned, when the caller ran
    the SMP lookup itself before locking the invoice (the route does); without
    it the lookup runs here, after validation.

    Commits the ``sending`` reservation before the transmit (releasing the
    caller's row lock), then commits the outcome.
    """
    # 1. Idempotency short-circuit (fast path; the DB index is the guarantee).
    existing = await _select_live_outbound(db, invoice.id)
    if existing is not None:
        return existing, True

    # 2-4. Map → validate (hard tax reject) → serialize. These raise BEFORE we
    # persist anything, so a tax-invalid invoice leaves no row behind.
    doc = invoice_to_einvoice_document(invoice, line_items, buyer)
    assert_valid(doc)  # check_tax=True by default → EInvoiceValidationError on fail
    # The parties' electronic addresses (BT-34 / BT-49) ARE the AS4 participant
    # ids: the receiver is the counterparty this invoice names as seller, the
    # sender is us — the document's buyer. They live only on the transport
    # arguments, so stamp them onto the document before it is serialized;
    # otherwise the UBL we transmit carries no `cbc:EndpointID` on either party
    # and cannot claim the profile the AS4 doc-type id below asserts.
    doc.seller.endpoint_id = receiver_id.value
    doc.seller.endpoint_scheme_id = receiver_id.scheme
    doc.buyer.endpoint_id = sender_id.value
    doc.buyer.endpoint_scheme_id = sender_id.scheme
    # We are about to put this document on the network under
    # PEPPOL_BIS_BILLING_DOCTYPE, which ASSERTS EN 16931 / BIS Billing 3.0
    # conformance. Refuse here rather than transmit a document that provably
    # does not meet it — the Access Point would reject it anyway, and a claim we
    # can disprove ourselves must never leave the building. PII-free
    # `field: code` body, mapped to 422 at the route.
    assert_bis3_conformant(doc)
    payload = generate_ubl(doc)

    # 5-6. Resolve the receiver via SMP/SML (mockable, no DNS) unless the
    # caller already did. Unknown → refuse before persisting a row.
    if adapter is None:
        adapter = await resolve_receiver(peppol_config, receiver_id)

    business_message_id = invoice.correlation_id.hex
    # Capture the id BEFORE the flush: an IntegrityError + rollback expires the
    # ORM `invoice`, so a later `invoice.id` would trigger a lazy reload (IO)
    # outside the async greenlet → MissingGreenlet. The local is rollback-safe,
    # and commit-safe for the reservation commit below.
    invoice_id = invoice.id
    correlation_id = invoice.correlation_id

    # 7. Claim the idempotency slot: INSERT 'sending' and flush BEFORE the
    # networked transmit. A concurrent send that already claimed it raises
    # IntegrityError on the partial unique index → return the committed live row.
    transmission = PeppolTransmission(
        invoice_id=invoice_id,
        direction=_DIRECTION_OUTBOUND,
        participant_scheme=receiver_id.scheme,
        participant_value=receiver_id.value,
        sender_scheme=sender_id.scheme,
        sender_value=sender_id.value,
        doc_type_id=PEPPOL_BIS_BILLING_DOCTYPE,
        process_id=PEPPOL_BIS_BILLING_PROCESSID,
        business_message_id=business_message_id,
        status="sending",
        provider=adapter.provider_name,
        amount=invoice.amount,
        currency=invoice.currency,
        organization_id=organization_id,
        entity_id=entity_id,
    )
    db.add(transmission)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        racer = await _select_live_outbound(db, invoice_id)
        if racer is not None:
            return racer, True
        raise
    # 7a. Audit the reservation in its own transaction (decisions §580): if the
    # process dies before the outcome commit, the row stays `sending` and this
    # is the trail's only record of who started the send and under which key.
    await dispatch_audit(
        db,
        correlation_id=correlation_id,
        organization_id=organization_id,
        actor_id=actor_id,
        action="invoice.peppol_send_started",
        entity_type="invoice",
        entity_id=invoice_id,
        details={
            "provider": adapter.provider_name,
            "receiver_scheme": receiver_id.scheme,
            "business_message_id": business_message_id,
            "doc_type": PEPPOL_BIS_BILLING_DOCTYPE,
        },
    )
    # 7b. Commit the reservation: the slot is claimed durably and the caller's
    # invoice row lock is released before the network is touched (§578).
    await db.commit()

    # 8. Transmit (the only networked step; the slot is already claimed).
    result = await adapter.send(
        TransmissionRequest(
            sender=sender_id,
            receiver=receiver_id,
            doc_type_id=PEPPOL_BIS_BILLING_DOCTYPE,
            process_id=PEPPOL_BIS_BILLING_PROCESSID,
            payload=payload,
            business_message_id=business_message_id,
        )
    )

    # 9. Update the row from the adapter outcome. A failed send NEVER persists a
    # message_id (defence in depth alongside the adapter): a non-NULL message_id
    # on a failed row would occupy `uq_peppol_message_id`, and the supported
    # failed→retry path reuses the same business_message_id — so a real AP that
    # echoes the same MessageId on the retry would collide on commit.
    transmission.status = result.status if result.success else "failed"
    transmission.message_id = result.message_id if result.success else None
    transmission.failure_reason = result.failure_reason
    transmission.raw_response = result.raw_response
    transmission.transmitted_at = datetime.now(UTC)

    # 10. Audit — PII-free details only (scheme, message id, doc type, provider).
    action = "invoice.peppol_sent" if result.success else "invoice.peppol_send_failed"
    await dispatch_audit(
        db,
        correlation_id=correlation_id,
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        entity_type="invoice",
        entity_id=invoice_id,
        details={
            "provider": adapter.provider_name,
            "receiver_scheme": receiver_id.scheme,
            "message_id": result.message_id,
            "doc_type": PEPPOL_BIS_BILLING_DOCTYPE,
            "status": transmission.status,
            "failure_reason": result.failure_reason,
        },
    )

    # 11. Commit.
    await db.commit()
    await db.refresh(transmission)
    return transmission, False


# ---------------------------------------------------------------------------
# Settling a `sending` row whose outcome was never recorded (decisions §580)
# ---------------------------------------------------------------------------

#: A ``sending`` row younger than this may still be in flight: its request is
#: inside the adapter's timeout (20 s for ``as4_gateway``) or its outcome commit
#: is a moment away. Settling it then would race the send it describes, so the
#: settle refuses. Thirty minutes is far past any send and short enough that a
#: stuck invoice is unblocked the same working session.
STALE_SENDING_AFTER = timedelta(minutes=30)


class SettleRefused(Exception):
    """The ``sending`` row was left as it was. ``code`` is PII-free:

    - ``no_sending_transmission`` — the invoice has no outbound row in ``sending``
    - ``sending_too_recent`` — younger than :data:`STALE_SENDING_AFTER`
    - ``provider_changed`` — the org's configured provider is not the one the
      row was sent through, so its Access Point cannot be asked
    - ``lookup_unsupported`` — the adapter cannot ask its Access Point
    - ``lookup_inconclusive`` — the Access Point could not answer
      (``detail`` carries its PII-free reason)
    - ``already_settled`` — the send's own outcome commit landed meanwhile
    """

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


@dataclass
class SettleResult:
    transmission_id: uuid.UUID
    status: str  # the row's new status: sent | delivered | failed
    lookup_outcome: str  # the Access Point's answer, LOOKUP_OUTCOMES


async def _sending_row(
    db: AsyncSession, invoice_id: uuid.UUID, *, for_update: bool = False
) -> PeppolTransmission | None:
    stmt = select(PeppolTransmission).where(
        PeppolTransmission.invoice_id == invoice_id,
        PeppolTransmission.direction == _DIRECTION_OUTBOUND,
        PeppolTransmission.status == "sending",
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def settle_stale_sending(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    peppol_config: dict | None,
    reason: str | None = None,
    now: datetime | None = None,
    min_age: timedelta = STALE_SENDING_AFTER,
) -> SettleResult:
    """Settle an invoice's stale ``sending`` transmission from the Access
    Point's own record of it.

    Asks the AP by the row's ``business_message_id`` (no lock held across the
    network), then, under the row lock and still ``sending``: ``sent`` /
    ``delivered`` record the AP's MessageId; ``failed`` or ``not_found`` mark
    the row ``failed`` (a ``not_found`` document never reached the AP, and the
    resend reuses the same key). Anything else refuses and changes nothing —
    an unanswered row is never marked failed. The ``invoice.peppol_send_settled``
    audit row is written in the same transaction, before the row changes; no
    actor (an operator, not an app user).
    """
    now = now or datetime.now(UTC)
    row = await _sending_row(db, invoice_id)
    if row is None:
        raise SettleRefused("no_sending_transmission")
    if now - row.created_at < min_age:
        raise SettleRefused("sending_too_recent")
    try:
        adapter = get_peppol_adapter(peppol_config)
    except UnknownPeppolProviderError:
        raise SettleRefused("provider_changed") from None
    if adapter.provider_name != row.provider:
        raise SettleRefused("provider_changed")
    transmission_id = row.id
    business_message_id = row.business_message_id
    await db.rollback()  # hold nothing open across the network

    try:
        lookup: TransmissionLookup = await adapter.lookup_transmission(business_message_id)
    except NotImplementedError:
        raise SettleRefused("lookup_unsupported") from None
    if lookup.outcome in ("sent", "delivered"):
        new_status = lookup.outcome
    elif lookup.outcome in ("failed", "not_found"):
        new_status = "failed"
    else:
        raise SettleRefused("lookup_inconclusive", lookup.failure_reason)

    row = await _sending_row(db, invoice_id, for_update=True)
    if row is None or row.id != transmission_id:
        await db.rollback()
        raise SettleRefused("already_settled")
    invoice = (
        await db.execute(
            select(Invoice.correlation_id, Invoice.organization_id).where(Invoice.id == invoice_id)
        )
    ).one()
    failure_reason = None
    if new_status == "failed":
        failure_reason = lookup.failure_reason or (
            "not_at_access_point" if lookup.outcome == "not_found" else "access_point_failed"
        )
    message_id = lookup.message_id if new_status != "failed" else None
    await dispatch_audit(
        db,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=None,
        action="invoice.peppol_send_settled",
        entity_type="invoice",
        entity_id=invoice_id,
        details={
            "provider": row.provider,
            "business_message_id": business_message_id,
            "lookup_outcome": lookup.outcome,
            "status": new_status,
            "message_id": message_id,
            "failure_reason": failure_reason,
            "sending_since": row.created_at.isoformat(),
            "reason": reason,
        },
    )
    row.status = new_status
    row.message_id = message_id
    row.failure_reason = failure_reason
    row.raw_response = lookup.raw_response
    row.transmitted_at = now if new_status != "failed" else row.transmitted_at
    await db.commit()
    return SettleResult(
        transmission_id=transmission_id, status=new_status, lookup_outcome=lookup.outcome
    )
