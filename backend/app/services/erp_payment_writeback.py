"""A payment FeohLedger made, written back to the ERP against its bill (§295).

Phase 4 of the QuickBooks Online adapter: once a FeohLedger payment has
settled and its invoice is ``paid``, ``payment_erp_sync`` hands the payment
here, and an ERP whose status sync is on for the tenant
(``erp_adapters.base.reports_status_back`` — QuickBooks Online, Xero, and
NetSuite only with its per-tenant opt-in, §541) records it as a payment of
the bill the invoice was posted as. "The bill shows paid in QuickBooks" is
what keeps a bookkeeper there from paying it a second time.

Rules this module owns:

* **Only a bill we posted, in the company we are connected to.** The bill id
  is the invoice's ``erp_reference``, and it is used only when the push
  recorded the same connection realm (``state_data.erp_realm``): ERP ids are
  per company (§284), so a bill id from the sandbox names something else in
  production. An invoice never pushed (paid on the direct-schedule path) has
  no bill and nothing is written.
* **Idempotent on the payment id.** The adapter looks for its own earlier write
  before creating one, and sends the payment id as the ERP's idempotency key —
  a retry (``POST /api/payments/runs/{id}/sync-erp``) never creates a second
  ERP payment.
* **The pay-from account is the org's choice, never guessed.**
  ``settings.erp.bill_payment_bank_account`` / ``bill_payment_card_account``
  name GL codes in our chart; each resolves to its ERP id under the current
  realm (``gl_chart.resolve_erp_account_ids``). Unmapped or unlinked → the
  adapter refuses ``payment_account_not_mapped``.
* **A refusal or failure is never silent.** It writes ``payment.erp_post_failed``
  (the coded refusal, ``intuit_code`` and all, never the ERP's text) and opens a
  de-duped ``erp_reconciliation`` exception; the money already moved, so it
  never blocks or undoes anything here.
* **A bill left partly open in the ERP is flagged** (a discount or credit
  taken in FeohLedger that the ERP does not know about) — the ERP's books
  would otherwise show the difference as still owed.
* **An early-payment discount is booked as a vendor credit only by the org's
  opt-in** (``settings.erp.book_discount_as_credit: "on"`` with
  ``discount_credit_account``, a GL code of ours, decisions §575): after the
  payment is written, the adapter books ``Payment.discount_amount`` as a credit
  applied to the bill, idempotent on the payment id. A refused or failed
  credit is audited coded (``payment.erp_discount_credit_failed``) and the
  bill's open balance is flagged exactly as without the opt-in, so a person
  books it. A void reverses our credit before it voids our payment, except a
  recorded credit edited in the ERP (``discount_credit_not_ours``), which is
  held and flagged while the payment is voided anyway (decisions §584).
* **Reserve -> call -> record, for the payment write too** (§584): a
  pending-write record is committed before ``post_payment`` and cleared with
  the recorded outcome; the sweep finishes one left behind.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.payment import Payment
from app.services.audit_dispatch import dispatch_audit
from app.services.erp_adapters.base import (
    DiscountCreditPayload,
    ErpAdapter,
    ErpPostResult,
    PaymentPayload,
    reports_status_back,
)
from app.services.erp_link_realm import MOCK_BILL_REALM, connection_realm, legacy_links_trusted
from app.services.erp_status_sync import erp_finding, open_erp_reconciliation_exception
from app.services.payment_methods import CARD_PAYMENT_METHODS
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

#: The ``settings.erp`` keys naming the GL accounts a written-back payment is
#: booked from: one for bank rails, one for card rails.
BANK_ACCOUNT_KEY = "bill_payment_bank_account"
CARD_ACCOUNT_KEY = "bill_payment_card_account"
#: The ``settings.erp`` opt-in (``"on"`` / ``"off"``, absent = off) that books
#: an early-payment discount as a vendor credit applied to the bill, and the GL
#: code (our chart) of the account it is booked to (decisions §575).
DISCOUNT_CREDIT_KEY = "book_discount_as_credit"
DISCOUNT_ACCOUNT_KEY = "discount_credit_account"


def discount_credit_on(erp_config: dict | None) -> bool:
    """Has the org opted in to booking discounts as credits? Only the literal
    ``"on"`` is yes: anything else, absent included, is the default off."""
    return str((erp_config or {}).get(DISCOUNT_CREDIT_KEY) or "").strip().lower() == "on"


#: ``erp_report["kind"]`` of the rows this module opens. The sentence is for an
#: AP user (``invoice_warning_catalog``); the payment, its run, the bill and the
#: refusal ride the report as data, never the sentence (decisions §319, §331).
ERP_REPORT_WRITEBACK_FAILED = "payment_writeback_failed"
ERP_REPORT_WRITEBACK_BALANCE_OPEN = "payment_writeback_balance_open"
ERP_REPORT_VOID_WRITEBACK_FAILED = "payment_void_writeback_failed"


def _writeback_report(
    kind: str,
    *,
    erp_type: str,
    payment_id: uuid.UUID,
    payment_run_id: uuid.UUID | None,
    bill_id: str,
    **extra: object,
) -> dict:
    """The PII-free machine detail of a write-back row: ids, the provider and,
    on a failure, the coded refusal (``erp_refusal_record``: a code and its
    params, never the ERP's text)."""
    return {
        "kind": kind,
        "erp_type": erp_type,
        "erp_document_id": bill_id,
        "payment_id": str(payment_id),
        "payment_run_id": str(payment_run_id) if payment_run_id else None,
        **extra,
    }


#: Outcomes of :func:`write_back_payment`.
WROTE = "wrote"
SKIPPED = "skipped"
FAILED = "failed"


def bill_for_writeback(state_data: dict | None, erp_config: dict | None) -> str | None:
    """The ERP bill this invoice was posted as, when it lives in the company
    ``erp_config`` connects to now — else None (nothing to write back to)."""
    state = state_data or {}
    bill_id = state.get("erp_reference")
    realm = connection_realm(erp_config)
    if not bill_id or bill_id == "UNKNOWN" or realm is None:
        return None
    if state.get("erp_realm") != realm:
        return None
    return str(bill_id)


async def build_payment_payload(
    db: AsyncSession,
    *,
    payment: Payment,
    invoice: Invoice,
    bill_id: str,
    erp_config: dict,
) -> PaymentPayload:
    card = (payment.method or "") in CARD_PAYMENT_METHODS
    code = str((erp_config or {}).get(CARD_ACCOUNT_KEY if card else BANK_ACCOUNT_KEY) or "")
    code = code.strip() or None
    account_erp_id = await _resolve_account(db, invoice, erp_config, code)
    paid_at = payment.completed_at
    currency = (invoice.currency or "").upper()
    # What reached the supplier: the rail's settled figure when it is in the
    # bill's currency (a short settlement accepted as final pays less than was
    # booked), else the booked amount.
    amount = Decimal(payment.amount)
    if payment.settled_amount is not None and (payment.settled_currency or "").upper() == currency:
        amount = Decimal(payment.settled_amount)
    return PaymentPayload(
        payment_id=str(payment.id),
        bill_id=bill_id,
        amount=amount,
        currency=currency,
        paid_on=paid_at.astimezone(UTC).date() if paid_at else utc_today(),
        method=payment.method,
        card=card,
        pay_from_account_erp_id=account_erp_id,
        reference=payment.reference,
    )


async def _resolve_account(
    db: AsyncSession, invoice: Invoice, erp_config: dict, code: str | None
) -> str | None:
    """Our GL ``code``'s ERP id under the current realm, or None."""
    from app.services.gl_chart import resolve_erp_account_ids

    if not code:
        return None
    realm = connection_realm(erp_config)
    ids = await resolve_erp_account_ids(
        db,
        invoice.organization_id,
        invoice.entity_id,
        [code],
        realm=realm,
        trust_legacy=legacy_links_trusted(erp_config, realm),
    )
    return ids.get(code)


async def discount_account_refusal(
    db: AsyncSession, organization_id: uuid.UUID, erp_config: dict | None
) -> str | None:
    """Why a ``settings.erp`` block about to be saved with the discount credit
    on names an account the write-back could not resolve, or None.

    Read with :func:`_resolve_account`'s rule on the org-wide (shared) chart:
    the account must exist there and carry an ERP id linked to the connection
    being saved (``connection_realm`` / ``legacy_links_trusted``). An
    entity-only account resolves for that entity's invoices alone, and one
    never synced, or synced from another ERP company, for none — a save that
    names one would turn on a credit every write-back then refuses
    (decisions §578). Off, or no account named, is
    ``catalog.discount_credit_refusal``'s call, not this one's.
    """
    from app.services.gl_chart import resolve_erp_account_ids

    if not discount_credit_on(erp_config):
        return None
    code = str((erp_config or {}).get(DISCOUNT_ACCOUNT_KEY) or "").strip()
    if not code:
        return None
    realm = connection_realm(erp_config)
    ids = await resolve_erp_account_ids(
        db,
        organization_id,
        None,
        [code],
        realm=realm,
        trust_legacy=legacy_links_trusted(erp_config, realm),
    )
    if ids.get(code):
        return None
    return (
        f"erp.{DISCOUNT_ACCOUNT_KEY} must be an organization-wide GL account linked to "
        "the connected ERP. Sync the chart of accounts, then pick it again."
    )


async def build_discount_credit_payload(
    db: AsyncSession,
    *,
    payment: Payment,
    invoice: Invoice,
    payment_payload: PaymentPayload,
    erp_config: dict,
) -> DiscountCreditPayload | None:
    """The credit to book for ``payment``'s early-payment discount, or None
    when the org has not opted in or the payment took no discount."""
    if not discount_credit_on(erp_config):
        return None
    discount = Decimal(payment.discount_amount) if payment.discount_amount else Decimal(0)
    if discount <= 0:
        return None
    code = str((erp_config or {}).get(DISCOUNT_ACCOUNT_KEY) or "").strip() or None
    return DiscountCreditPayload(
        payment_id=payment_payload.payment_id,
        bill_id=payment_payload.bill_id,
        amount=discount,
        currency=payment_payload.currency,
        credit_on=payment_payload.paid_on,
        discount_account_erp_id=await _resolve_account(db, invoice, erp_config, code),
        pay_from_account_erp_id=payment_payload.pay_from_account_erp_id,
        card=payment_payload.card,
    )


def _refusal(result: ErpPostResult) -> dict | None:
    if not result.refusal_code:
        return None
    from app.services.erp import erp_refusal_record

    return erp_refusal_record(
        result.refusal_code, provider=result.refusal_provider, extra=result.refusal_params
    )


async def write_back_payment(
    db: AsyncSession,
    *,
    adapter: ErpAdapter,
    erp_config: dict,
    payment: Payment,
    invoice: Invoice,
    state_data: dict | None,
) -> str:
    """Write one settled payment back to the ERP. Commits. Never raises.

    The caller has committed the invoice's ``paid`` transition first, so no
    row lock is held across the ERP call.
    """
    if not reports_status_back(adapter):
        return SKIPPED
    bill_id = bill_for_writeback(state_data, erp_config)
    if bill_id is None:
        return SKIPPED
    from sqlalchemy import select

    # Re-read: a void can commit between the caller's transition and here, and
    # a BillPayment for a voided payment would show the bill paid in the ERP.
    current = (
        await db.execute(select(Payment.status).where(Payment.id == payment.id))
    ).scalar_one_or_none()
    if current != "completed":
        await db.commit()
        return SKIPPED
    correlation = invoice.correlation_id or payment.correlation_id or invoice.id
    payment_key = payment.id
    credit_payload: DiscountCreditPayload | None = None
    credit: ErpPostResult | None = None
    held = False
    reserved = False
    claimed: dict | None = None
    # A credit an earlier attempt left standing (decisions §577) is THE credit
    # for this payment: a re-run (manual sync, a webhook re-dispatch) finishes
    # that one by its recorded id and amount, never books a second (§579).
    recorded = _pending_record(await _state_data(db, invoice.id), payment.id)
    write_token: str | None = None
    try:
        payload = await build_payment_payload(
            db, payment=payment, invoice=invoice, bill_id=bill_id, erp_config=erp_config
        )
        if getattr(adapter, "supports_discount_credit", False) is True:
            if recorded is not None:
                credit_payload = _payload_from_marker(str(payment.id), recorded)
            else:
                credit_payload = await build_discount_credit_payload(
                    db,
                    payment=payment,
                    invoice=invoice,
                    payment_payload=payload,
                    erp_config=erp_config,
                )
        # Reserve -> call -> record for the payment write itself (decisions
        # §584): a pending-write record is committed under the locks BEFORE
        # the ERP is asked, and cleared only in the transaction that records
        # the outcome. If that transaction rolls back or the process dies, the
        # sweep (:func:`resume_pending_writes`) finds the record. The commit
        # also ends the read transaction: no connection is held open across
        # the ERP call (the caller's session does not expire rows on commit).
        write_token = await _reserve_write(
            db,
            invoice,
            payment.id,
            bill_id,
            erp_type=adapter.erp_type,
            payment_run_id=payment.payment_run_id,
        )
        if write_token is None:
            # Voided under the lock, another process's live claim, or the
            # attempts cap: nothing is sent.
            return SKIPPED
        result = await adapter.post_payment(payload)
    except Exception as exc:  # noqa: BLE001 — an adapter bug is a failed write-back
        result = ErpPostResult(success=False, message=f"write-back error: {exc.__class__.__name__}")
    if result.success and credit_payload is not None:
        # Only after our payment stands on the bill: the credit is capped by
        # what the bill still has open AFTER it (the adapter refuses more).
        # Reserve -> call -> record (decisions §581): a first attempt commits
        # its record BEFORE the create, so a crash or a failed recording
        # leaves a record the next attempt can only look up, never re-create;
        # a re-run claims the record under the lock and builds its payload
        # from what it claimed, never from the unlocked read above.
        status: str | None = None
        try:
            if recorded is None:
                claimed, status, reserved = await _reserve_credit(
                    db, invoice.id, payment.id, credit_payload
                )
            else:
                claimed, status = await _claim_record(db, invoice.id, payment.id)
        except Exception as exc:  # noqa: BLE001 — no claim: no ERP call for the credit
            await db.rollback()
            claimed = None
            logger.warning(
                "[payment-writeback] could not claim the discount credit of payment %s: %s",
                payment.id,
                exc.__class__.__name__,
            )
        if claimed is None:
            credit_payload = None  # nothing to do here, or another process holds it
        elif reserved:
            credit = await _call_credit(adapter, credit_payload)
        else:
            credit_payload = _payload_from_marker(str(payment.id), claimed)
            credit, held = await _call_recorded_credit(
                adapter, credit_payload, completed=status == "completed"
            )

    try:
        if result.success:
            # A void that committed after the status check above and whose
            # own ERP void ran before this BillPayment existed found nothing
            # to void: re-arm it, so the sweep voids what was just written
            # (decisions §581).
            void_rearmed = await _rearm_void_if_voided(db, invoice.id, payment.id)
            await _clear_write(db, invoice.id, payment.id, write_token)
            await dispatch_audit(
                db,
                correlation_id=correlation,
                organization_id=invoice.organization_id,
                actor_id=None,
                action="payment.erp_posted",
                entity_type="payment",
                entity_id=payment.id,
                details={
                    "erp_type": adapter.erp_type,
                    "bill_id": bill_id,
                    "erp_document_id": result.erp_document_id,
                    "bill_balance_after": (
                        str(result.bill_balance_after)
                        if result.bill_balance_after is not None
                        else None
                    ),
                    **({"void_rearmed": True} if void_rearmed else {}),
                },
            )
            balance_after = result.bill_balance_after
            credit_refusal = None
            kept: dict | None = None
            if credit is not None:
                credit_refusal = _refusal(credit)
                if credit.success and credit.bill_balance_after is not None:
                    balance_after = credit.bill_balance_after
                # A credit that may stand in the ERP (created, its application
                # failed or unconfirmed) stays recorded so the sweep finishes
                # THAT one and nobody books a second (decisions §577, §579).
                kept = await _record_credit_outcome(
                    db,
                    adapter,
                    invoice,
                    payment.id,
                    credit_payload,
                    credit,
                    prior=None if reserved else claimed,
                    held=held,
                    reserved=reserved,
                    payment_run_id=payment.payment_run_id,
                )
            if balance_after is not None and balance_after > 0:
                standing = await _standing_credit(db, adapter, invoice.id, payment.id, kept, credit)
                await open_erp_reconciliation_exception(
                    db,
                    invoice,
                    org_id=invoice.organization_id,
                    finding=_balance_open_finding(
                        adapter.erp_type, payment.id, standing, balance_after, invoice.currency
                    ),
                    erp_report=_writeback_report(
                        ERP_REPORT_WRITEBACK_BALANCE_OPEN,
                        erp_type=adapter.erp_type,
                        payment_id=payment.id,
                        payment_run_id=payment.payment_run_id,
                        bill_id=bill_id,
                        erp_payment_id=result.erp_document_id,
                        bill_balance_after=str(balance_after),
                        # An early-payment discount taken here is the usual
                        # reason: no ERP payment carries one (§295, §386), so
                        # the bookkeeper records it there.
                        discount_amount=(
                            str(payment.discount_amount) if payment.discount_amount else None
                        ),
                        # The opt-in's credit, when it was tried and did not
                        # book: its coded reason, so the person sees why (§575).
                        **(
                            {
                                "discount_credit_failure": (
                                    credit_refusal["code"]
                                    if credit_refusal
                                    else "erp_discount_credit_failed"
                                )
                            }
                            if credit is not None and not credit.success
                            else {}
                        ),
                        # The credit may already be in the ERP: name it, and
                        # say a retry will apply it — booking another by hand
                        # would take the discount twice (§577).
                        **(
                            {
                                "discount_credit_pending": True,
                                "discount_credit_document_id": standing.get("credit_id"),
                            }
                            if standing is not None
                            else {}
                        ),
                    ),
                )
            await db.commit()
            return WROTE

        refusal = _refusal(result)
        # Only a definite refusal before any write (``not_posted``) says
        # nothing stands in the ERP: then the failure is the whole outcome and
        # the record goes. Any other failure (a network drop after the send, a
        # 5xx on the read-back, an adapter error) may have left our payment on
        # the bill: the record is kept, its claim released, for
        # :func:`resume_pending_writes` (whose adapter write finds our own
        # earlier payment before it creates), and a void that raced the send
        # is re-armed as on success (decisions §588).
        unconfirmed = not result.not_posted
        if unconfirmed:
            void_rearmed = await _rearm_void_if_voided(db, invoice.id, payment.id)
            await _keep_write(db, invoice.id, payment.id, write_token, drop=void_rearmed)
        else:
            await _clear_write(db, invoice.id, payment.id, write_token)
        await dispatch_audit(
            db,
            correlation_id=correlation,
            organization_id=invoice.organization_id,
            actor_id=None,
            action="payment.erp_post_failed",
            entity_type="payment",
            entity_id=payment.id,
            details={
                "erp_type": adapter.erp_type,
                "bill_id": bill_id,
                # PII-free by construction: a fixed literal, an HTTP code and
                # a reason code (`base.erp_failure_message` / `erp_refusal`).
                "error": result.message,
                "retryable": result.retryable,
                **({"refusal": refusal} if refusal else {}),
            },
        )
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=invoice.organization_id,
            finding=_write_failed_finding(adapter.erp_type, payment.id, unconfirmed=unconfirmed),
            erp_report=_writeback_report(
                ERP_REPORT_WRITEBACK_FAILED,
                erp_type=adapter.erp_type,
                payment_id=payment.id,
                payment_run_id=payment.payment_run_id,
                bill_id=bill_id,
                failure=refusal["code"] if refusal else "erp_post_failed",
                refusal=refusal,
                retryable=result.retryable,
                # A string, not a bool: the dedupe compares the JSON text.
                **({"outcome": WRITE_UNCONFIRMED} if unconfirmed else {}),
            ),
            dedupe_on=("outcome",),
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — recording must never fail the leg
        await db.rollback()
        # Not ``payment.id``: the rollback expired the row, and reading it
        # here would lazy-load outside the greenlet (MissingGreenlet), raise
        # out of this never-raising function, and flag a sync failure on a
        # payment that synced (decisions §584). Its pending-write record is
        # what the sweep finishes.
        logger.warning(
            "[payment-writeback] could not record the outcome for payment %s: %s",
            payment_key,
            exc.__class__.__name__,
        )
    return FAILED


# ---------------------------------------------------------------------------
# Writing back from a path other than the sync pass
# ---------------------------------------------------------------------------

_tasks: set[asyncio.Task] = set()


def _spawn(coro, name: str) -> None:
    """Run on the caller's loop, off the response (the ``payment_erp_sync``
    shape); asyncio keeps only a weak reference to a task, hence the set."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _org_adapter(org_id: uuid.UUID):
    """``(org, resolved erp config, adapter)`` for an org whose ERP reports
    back, else None."""
    from app.database import control_session_factory
    from app.models.organization import Organization
    from app.services.erp_adapters import get_erp_adapter
    from app.services.provider_credentials import provider_config

    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, org_id)
        if org is None:
            return None
        erp_config = await provider_config(org, "erp", db=ctrl) or None
    if not erp_config:
        return None
    adapter = get_erp_adapter(erp_config)
    if not reports_status_back(adapter):
        return None
    return org, erp_config, adapter


async def _state_data(db: AsyncSession, invoice_id: uuid.UUID) -> dict | None:
    from sqlalchemy import select

    from app.models.workflow import WorkflowInstance

    return (
        await db.execute(
            select(WorkflowInstance.state_data).where(WorkflowInstance.invoice_id == invoice_id)
        )
    ).scalar_one_or_none()


def dispatch_settled_writeback(org_id: uuid.UUID, payment_id: uuid.UUID) -> None:
    """A FeohLedger payment reached ``paid`` outside the sync pass (a short
    settlement accepted as final): write it back the same way, after the
    caller's commit."""
    _spawn(write_back_settled_payment(org_id, payment_id), f"writeback-{payment_id}")


async def write_back_settled_payment(org_id: uuid.UUID, payment_id: uuid.UUID) -> str:
    """:func:`write_back_payment` for one payment, loading what it needs.
    Never raises."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine

    try:
        found = await _org_adapter(org_id)
        if found is None:
            return SKIPPED
        org, erp_config, adapter = found
        factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
        async with factory() as db:
            payment = await db.get(Payment, payment_id)
            invoice = await db.get(Invoice, payment.invoice_id) if payment else None
            if invoice is None or invoice.status.value != "paid":
                return SKIPPED
            state = await _state_data(db, invoice.id)
            return await write_back_payment(
                db,
                adapter=adapter,
                erp_config=erp_config,
                payment=payment,
                invoice=invoice,
                state_data=state,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort, after the caller committed
        logger.warning(
            "[payment-writeback] write-back failed for payment %s: %s",
            payment_id,
            exc.__class__.__name__,
        )
        return FAILED


# ---------------------------------------------------------------------------
# A voided payment: void what we wrote
# ---------------------------------------------------------------------------

#: ``WorkflowInstance.state_data`` key: ids of voided payments whose ERP
#: payment still has to be voided. Written in the void's own transaction, so a
#: process that dies before the background task runs leaves the intent behind,
#: and the status-sync sweep (:func:`resume_pending_voids`) finishes it.
PENDING_VOIDS_KEY = "erp_payment_voids_pending"


#: ``WorkflowInstance.state_data`` key: payment id → a discount credit that may
#: stand in the ERP unapplied (its create succeeded or is unknown, its
#: application did not), with what the retry needs to apply it: the credit's
#: ERP id when known, and the payload's PII-free fields (decisions §577). The
#: status-sync sweep (:func:`resume_pending_credits`) finishes it.
PENDING_CREDITS_KEY = "erp_discount_credits_pending"

#: ``WorkflowInstance.state_data`` key: payment id → the ERP id of our discount
#: credit, recorded BEFORE a void deletes anything, so a void that removed the
#: credit's application but not the credit can find it on retry (§577).
PENDING_VOID_CREDITS_KEY = "erp_discount_credit_voids_pending"

#: ``WorkflowInstance.state_data`` key: payment id → the ERP id of the discount
#: credit a write-back BOOKED (outcome ``posted``). The void reverses it by this
#: id: Xero's lookup by number is unconfirmed, and a miss there read as
#: "nothing of ours" would void the payment over an allocated credit. Cleared
#: when the void reverses it. Not a pending key for the delete door: while it
#: matters (a voided payment not yet reversed) :data:`PENDING_VOIDS_KEY` holds
#: the payment too, and a completed payment's invoice is not deletable
#: (decisions §582).
BOOKED_CREDITS_KEY = "erp_discount_credits_booked"

#: ``WorkflowInstance.state_data`` key: payment id → a fresh token each time a
#: write-back re-arms that payment's void (:func:`_rearm_void_if_voided`). The
#: id is usually already in :data:`PENDING_VOIDS_KEY`, so the re-arm alone
#: changes nothing a void that is mid-call could see; the void reads the token
#: before its ERP call and clears the payment only if it is unchanged
#: (decisions §582).
VOID_REARMED_KEY = "erp_payment_void_rearmed"

#: ``WorkflowInstance.state_data`` key: payment id → the payment write in
#: flight (``token``, ``bill_id``, ``attempts``, ``claimed_until``), committed
#: BEFORE ``post_payment`` and cleared in the transaction that records the
#: outcome (decisions §584). A record left behind (that transaction rolled
#: back, or the process died) is finished by :func:`resume_pending_writes`.
#: A pending key for the delete door: while it stands, a BillPayment of ours
#: may be on the bill with nothing recorded about it.
PENDING_WRITES_KEY = "erp_payment_writes_pending"
#: Attempts at one pending payment write before nothing sends it again: each
#: one's outcome went unrecorded, so a person is told (audited and flagged
#: once). The same 12 as a recorded credit (§579).
MAX_WRITE_ATTEMPTS = 12

#: ``WorkflowInstance.state_data`` key: payment id → ``{outcome, attempts}``
#: of a payment's ERP void that keeps failing. ``payment.erp_void_failed`` is
#: audited only when the outcome changes (or the cap is reached), not every
#: sweep pass; at :data:`MAX_VOID_ATTEMPTS` nothing calls the ERP for it
#: again and the flag says so. Cleared when the void finishes, and reset when
#: a write-back re-arms it (decisions §584).
VOID_ATTEMPTS_KEY = "erp_payment_void_attempts"
MAX_VOID_ATTEMPTS = 12
#: ``erp_report["failure"]`` of the flag a capped write or void opens; resolving
#: that flag (a person, not an agent) clears the cap (decisions §588).
RETRIES_EXHAUSTED = "retries_exhausted"
#: ``erp_report["outcome"]`` of a write-back flag whose payment may stand in
#: the ERP (unknown outcome, the cap, a bill that moved): its own row, never
#: deduped against a definite refusal's (decisions §588).
WRITE_UNCONFIRMED = "unconfirmed"
#: ``erp_report["failure"]`` when a pending write names another bill than the
#: one its invoice points at now: kept, never retried there (decisions §588).
WRITE_BILL_CHANGED = "bill_changed"


def _credit_marker(payload: DiscountCreditPayload, credit_id: str | None) -> dict:
    return {
        "credit_id": str(credit_id) if credit_id else None,
        "bill_id": payload.bill_id,
        "amount": str(payload.amount),
        "currency": payload.currency,
        "credit_on": payload.credit_on.isoformat(),
        "discount_account_erp_id": payload.discount_account_erp_id,
        "pay_from_account_erp_id": payload.pay_from_account_erp_id,
        "card": bool(payload.card),
    }


def _payload_from_marker(payment_id: str, marker: dict) -> DiscountCreditPayload:
    from datetime import date

    return DiscountCreditPayload(
        payment_id=str(payment_id),
        bill_id=str(marker["bill_id"]),
        amount=Decimal(str(marker["amount"])),
        currency=str(marker["currency"]),
        credit_on=date.fromisoformat(str(marker["credit_on"])),
        discount_account_erp_id=marker.get("discount_account_erp_id"),
        pay_from_account_erp_id=marker.get("pay_from_account_erp_id"),
        card=bool(marker.get("card")),
        credit_id=marker.get("credit_id"),
    )


def _write_entry(instance, key: str, payment_id: uuid.UUID, value) -> None:
    """Set (or, with ``value`` None, drop) ``state_data[key][payment_id]`` on a
    locked instance."""
    state = dict(instance.state_data or {})
    entries = dict(state.get(key) or {})
    if value is None:
        if str(payment_id) not in entries:
            return
        entries.pop(str(payment_id), None)
    else:
        entries[str(payment_id)] = value
    if entries:
        state[key] = entries
    else:
        state.pop(key, None)
    instance.state_data = state


async def _lock_invoice_key_share(db: AsyncSession, invoice_id: uuid.UUID) -> None:
    """``FOR KEY SHARE`` on the invoice row, taken BEFORE its workflow
    instance's lock (decisions §581).

    The doors that touch the same state (payment void, invoice delete) lock
    the invoice ``FOR UPDATE`` and then the instance; this module locked the
    instance and then, through the ``exceptions.invoice_id`` FK of a flag it
    opens, the invoice — a cycle Postgres breaks by killing one side, usually
    the sweep, whose outcome then rolled back. Taking the invoice first, in
    the weakest mode that still waits for a door's ``FOR UPDATE``, makes the
    order one way. It is the mode the FK insert takes anyway, so it blocks
    nothing that insert did not already block: plain updates of the invoice
    (``FOR NO KEY UPDATE``) and other sweeps are compatible with it."""
    from sqlalchemy import select

    await db.execute(
        select(Invoice.id)
        .where(Invoice.id == invoice_id)
        .with_for_update(read=True, key_share=True)
    )


async def _lock_instance(db: AsyncSession, invoice_id: uuid.UUID):
    """The invoice's workflow instance under its row lock, read fresh — the
    invoice locked first (:func:`_lock_invoice_key_share`)."""
    from sqlalchemy import select

    from app.models.workflow import WorkflowInstance

    await _lock_invoice_key_share(db, invoice_id)
    return (
        await db.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.invoice_id == invoice_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _set_map_entry(
    db: AsyncSession, invoice_id: uuid.UUID, key: str, payment_id: uuid.UUID, value
) -> None:
    """Set (or, with ``value`` None, drop) ``state_data[key][payment_id]``
    under the instance's row lock. Never commits."""
    instance = await _lock_instance(db, invoice_id)
    if instance is not None:
        _write_entry(instance, key, payment_id, value)


async def _set_pending_credit(
    db: AsyncSession, invoice_id: uuid.UUID, payment_id: uuid.UUID, marker: dict | None
) -> None:
    await _set_map_entry(db, invoice_id, PENDING_CREDITS_KEY, payment_id, marker)


#: Attempts at one recorded discount credit (write-back re-runs and sweep
#: passes together) before nothing calls the ERP for it again: 12, three days
#: at the default six-hour status-sync interval. The record and its flag stay
#: for a person (decisions §579).
MAX_CREDIT_ATTEMPTS = 12
#: How long one process's claim on a recorded credit holds off every other
#: (the sweep's tick, ``sync_org_now``, a write-back re-run): longer than one
#: attempt's ERP calls can take (a handful of 15–30 s requests). A claim left
#: by a process that died lapses after it (decisions §579).
CREDIT_CLAIM_SECONDS = 300
#: ``erp_report["kind"]`` of the row a recorded credit opens when FeohLedger
#: cannot finish it alone: no known id, or the attempts cap reached (§579).
ERP_REPORT_DISCOUNT_CREDIT_STALLED = "discount_credit_stalled"
#: The failure message's code when a recorded credit's id is unknown and the
#: ERP lookup did not find it: nothing is created over it (§579).
DISCOUNT_CREDIT_UNIDENTIFIED = "discount_credit_unidentified"
#: The failure message's code when the recorded credit id reads back without
#: our marker (Xero may keep its own number on our create, §581's residual):
#: the record is held and flagged, never dropped as someone's void nor booked
#: over (decisions §582).
DISCOUNT_CREDIT_NOT_OURS = "discount_credit_not_ours"


def _pending_record(state: dict | None, payment_id) -> dict | None:
    record = ((state or {}).get(PENDING_CREDITS_KEY) or {}).get(str(payment_id))
    return dict(record) if isinstance(record, dict) else None


async def _standing_credit(
    db: AsyncSession,
    adapter: ErpAdapter,
    invoice_id: uuid.UUID,
    payment_id: uuid.UUID,
    kept: dict | None,
    credit: ErpPostResult | None,
) -> dict | None:
    """The record of a discount credit of ours that may stand on the bill while
    it shows a balance open (decisions §585): the one this attempt kept, or —
    when this attempt made none (another process holds the record's claim, or
    it is exhausted) — the record as it is now. None: no credit of ours can
    exist, so the open balance is the bookkeeper's to close."""
    if kept is not None:
        return kept
    if credit is not None or getattr(adapter, "supports_discount_credit", False) is not True:
        return None
    return _pending_record(await _state_data(db, invoice_id), payment_id)


def _balance_open_finding(
    erp_type: str | None,
    payment_id: uuid.UUID,
    standing: dict | None,
    amount: Decimal,
    currency: str | None,
) -> dict:
    """The open-balance sentence. While a credit of ours may stand it must not
    ask anyone to close the difference — a person would book the discount a
    second time (decisions §585):

    * no record → ``erp_payment_writeback_balance_open`` ("close the difference");
    * a record by id that FeohLedger will finish → ``erp_discount_credit_pending``
      naming the credit ("don't book another");
    * a record FeohLedger cannot finish alone (no id, retries exhausted, the id
      not ours — the cases the stall flag also opens for) →
      ``erp_discount_credit_pending_unidentified`` naming the marker to check for.
    """
    if standing is None:
        return erp_finding(
            "erp_payment_writeback_balance_open", erp_type, amount=amount, currency=currency
        )
    credit_id = standing.get("credit_id")
    finishes = (
        credit_id
        and not standing.get("exhausted")
        and not str(standing.get("outcome") or "").endswith(DISCOUNT_CREDIT_NOT_OURS)
    )
    if finishes:
        return erp_finding(
            "erp_discount_credit_pending",
            erp_type,
            amount=amount,
            currency=currency,
            credit=str(credit_id),
        )
    return erp_finding(
        "erp_discount_credit_pending_unidentified",
        erp_type,
        amount=amount,
        currency=currency,
        marker=f"FeohLedger discount {payment_id}",
    )


def _claim_live(record: dict, now) -> bool:
    from datetime import datetime

    until = record.get("claimed_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(str(until)) > now
    except ValueError:
        return False


async def _payment_row(db: AsyncSession, payment_id: uuid.UUID):
    """``(status, payment_run_id)`` of the payment, or ``(None, None)``."""
    from sqlalchemy import select

    row = (
        await db.execute(
            select(Payment.status, Payment.payment_run_id).where(Payment.id == payment_id)
        )
    ).one_or_none()
    return (row[0], row[1]) if row else (None, None)


async def _claim_record(
    db: AsyncSession, invoice_id: uuid.UUID, payment_id: uuid.UUID
) -> tuple[dict | None, str | None]:
    """Phase one of an attempt at a recorded credit: under the instance's row
    lock, re-read the record and the payment, and claim the record for
    :data:`CREDIT_CLAIM_SECONDS` — committed, so no lock is held across the
    ERP call. ``(None, status)`` when there is nothing to do here: no record,
    another process's live claim, the attempts cap reached, or a payment no
    longer ``completed`` whose credit id is known (its void reverses it by
    that id). Commits; never rolls back (a rollback would expire the caller's
    loaded rows)."""
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    instance = await _lock_instance(db, invoice_id)
    record = _pending_record(instance.state_data if instance else None, payment_id)
    status, _run = await _payment_row(db, payment_id)
    if (
        record is None
        or record.get("exhausted")
        or _claim_live(record, now)
        or (status != "completed" and record.get("credit_id"))
    ):
        await db.commit()
        return None, status
    until = (now + timedelta(seconds=CREDIT_CLAIM_SECONDS)).isoformat()
    _write_entry(instance, PENDING_CREDITS_KEY, payment_id, {**record, "claimed_until": until})
    await db.commit()
    return record, status


async def _reserve_credit(
    db: AsyncSession,
    invoice_id: uuid.UUID,
    payment_id: uuid.UUID,
    payload: DiscountCreditPayload,
) -> tuple[dict | None, str | None, bool]:
    """Phase one of a FIRST credit attempt (decisions §581): under the locks,
    commit a :data:`PENDING_CREDITS_KEY` record with no credit id, claimed
    for :data:`CREDIT_CLAIM_SECONDS`, before the ERP is asked to create.

    Returns ``(record, payment status, reserved)``. ``reserved`` True: the
    caller creates, then records the outcome over this record. A record that
    is already there (another write-back or the sweep got first) is claimed
    instead (:func:`_claim_record`) and the caller takes the recorded path,
    which never creates. ``(None, status, False)``: nothing to do — the
    payment is no longer ``completed`` or another process holds the claim.
    Commits; never rolls back."""
    from datetime import datetime, timedelta

    instance = await _lock_instance(db, invoice_id)
    if instance is not None and _pending_record(instance.state_data, payment_id) is not None:
        await db.commit()
        record, status = await _claim_record(db, invoice_id, payment_id)
        return record, status, False
    status, _run = await _payment_row(db, payment_id)
    booked = ((instance.state_data if instance else None) or {}).get(BOOKED_CREDITS_KEY) or {}
    if instance is None or status != "completed" or str(payment_id) in booked:
        # A credit already booked for this payment is THE credit: a re-run
        # never books a second (decisions §582).
        await db.commit()
        return None, status, False
    until = (datetime.now(UTC) + timedelta(seconds=CREDIT_CLAIM_SECONDS)).isoformat()
    record = {**_credit_marker(payload, None), "attempts": 0, "claimed_until": until}
    _write_entry(instance, PENDING_CREDITS_KEY, payment_id, record)
    await db.commit()
    return record, status, True


async def _rearm_void_if_voided(
    db: AsyncSession, invoice_id: uuid.UUID, payment_id: uuid.UUID
) -> bool:
    """After our ERP payment is written, under the locks: if the payment has
    been voided meanwhile, put it back in :data:`PENDING_VOIDS_KEY` so the
    sweep voids what was just written. The void's own task may have run
    before our write existed and found nothing (decisions §581). Never
    commits. Returns whether it re-armed."""
    instance = await _lock_instance(db, invoice_id)
    if instance is None:
        return False
    status, _run = await _payment_row(db, payment_id)
    if status == "completed":
        return False
    state = dict(instance.state_data or {})
    voids = [str(p) for p in state.get(PENDING_VOIDS_KEY) or []]
    if str(payment_id) not in voids:
        voids.append(str(payment_id))
    instance.state_data = {**state, PENDING_VOIDS_KEY: voids}
    # The id is usually still pending (the void's task has not cleared it
    # yet), so adding it changes nothing that task can see: a fresh token
    # does, and the task clears the payment only if the token it read before
    # its ERP call is unchanged (decisions §582).
    _write_entry(instance, VOID_REARMED_KEY, payment_id, uuid.uuid4().hex)
    # A re-armed void is a new piece of work: its attempts start again, so a
    # void capped on an earlier cause still voids what was just written (§584).
    _write_entry(instance, VOID_ATTEMPTS_KEY, payment_id, None)
    return True


def _write_record(state: dict | None, payment_id) -> dict | None:
    record = ((state or {}).get(PENDING_WRITES_KEY) or {}).get(str(payment_id))
    return dict(record) if isinstance(record, dict) else None


async def _reserve_write(
    db: AsyncSession,
    invoice: Invoice,
    payment_id: uuid.UUID,
    bill_id: str,
    *,
    erp_type: str,
    payment_run_id: uuid.UUID | None = None,
) -> str | None:
    """Phase one of a payment write (decisions §584): under the locks
    (invoice ``FOR KEY SHARE``, then the instance, §581), re-read the payment
    and commit a :data:`PENDING_WRITES_KEY` record naming this attempt's
    token, claimed for :data:`CREDIT_CLAIM_SECONDS`, BEFORE ``post_payment``.

    Returns the token, or None when nothing may be sent: the payment is no
    longer ``completed``, another process holds a live claim, or the record
    reached :data:`MAX_WRITE_ATTEMPTS` (audited and flagged once, when it
    does). A record a dead attempt left is claimed again: the adapter's write
    is idempotent on the payment id (it finds our earlier BillPayment before
    it creates), so the retry records what stands, never posts twice.
    Commits; never rolls back."""
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    instance = await _lock_instance(db, invoice.id)
    status, _run = await _payment_row(db, payment_id)
    record = _write_record(instance.state_data if instance else None, payment_id)
    if (
        instance is None
        or status != "completed"
        or (record is not None and (record.get("exhausted") or _claim_live(record, now)))
        # A record for another bill than the invoice's now: a write here would
        # search and pay the wrong bill (decisions §588). The sweep flags it.
        or (
            record is not None
            and record.get("bill_id") is not None
            and str(record["bill_id"]) != str(bill_id)
        )
    ):
        await db.commit()
        return None
    attempts = int((record or {}).get("attempts") or 0)
    if attempts >= MAX_WRITE_ATTEMPTS:
        _write_entry(instance, PENDING_WRITES_KEY, payment_id, {**record, "exhausted": True})
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id or invoice.id,
            organization_id=invoice.organization_id,
            actor_id=None,
            action="payment.erp_post_failed",
            entity_type="payment",
            entity_id=payment_id,
            details={
                "erp_type": erp_type,
                "bill_id": bill_id,
                "error": "write-back outcome never recorded",
                "attempts": attempts,
                "retries_exhausted": True,
            },
        )
        # Each attempt may have posted: the sentence asks a person to look for
        # our payment, never to retry or to record one (decisions §588). Its
        # own row, never swallowed by an earlier failure's; resolving it lets
        # the sweep try again (:func:`rearm_capped_on_resolve`).
        await open_erp_reconciliation_exception(
            db,
            invoice,
            org_id=invoice.organization_id,
            finding=_write_failed_finding(erp_type, payment_id, unconfirmed=True),
            erp_report=_writeback_report(
                ERP_REPORT_WRITEBACK_FAILED,
                erp_type=erp_type,
                payment_id=payment_id,
                payment_run_id=payment_run_id,
                bill_id=bill_id,
                failure=RETRIES_EXHAUSTED,
                attempts=attempts,
                outcome=WRITE_UNCONFIRMED,
            ),
            dedupe_on=("outcome", "failure"),
        )
        await db.commit()
        return None
    token = uuid.uuid4().hex
    until = (now + timedelta(seconds=CREDIT_CLAIM_SECONDS)).isoformat()
    _write_entry(
        instance,
        PENDING_WRITES_KEY,
        payment_id,
        {
            "token": token,
            "bill_id": bill_id,
            "attempts": attempts + 1,
            "claimed_until": until,
        },
    )
    await db.commit()
    return token


def _write_failed_finding(erp_type: str, payment_id, *, unconfirmed: bool) -> dict:
    """The sentence of a payment write that did not land. A definite refusal
    before any write: the bill still shows unpaid, retry once fixed. Anything
    else (an unknown outcome, the attempts cap, a record whose bill moved): our
    payment may stand, so the person checks for it by its marker before
    recording one — "retry" or "unpaid" there could pay the bill twice in the
    ERP's books (decisions §588)."""
    if not unconfirmed:
        return erp_finding("erp_payment_writeback_failed", erp_type)
    return erp_finding(
        "erp_payment_writeback_unconfirmed", erp_type, marker=f"FeohLedger payment {payment_id}"
    )


async def _keep_write(
    db: AsyncSession,
    invoice_id: uuid.UUID,
    payment_id: uuid.UUID,
    token: str | None,
    *,
    drop: bool = False,
) -> None:
    """In the transaction that records an UNKNOWN outcome: keep the
    pending-write record (only while it still names this attempt's token), its
    claim released so the sweep or a re-run tries again at once — or, with
    ``drop`` (the void was just re-armed and now owns what may stand), drop it
    as :func:`resume_pending_writes` would (decisions §588). Never commits."""
    if token is None:
        return
    instance = await _lock_instance(db, invoice_id)
    if instance is None:
        return
    record = _write_record(instance.state_data, payment_id)
    if record is None or record.get("token") != token:
        return
    if drop:
        _write_entry(instance, PENDING_WRITES_KEY, payment_id, None)
        return
    kept = {k: v for k, v in record.items() if k not in ("claimed_until", "token")}
    _write_entry(instance, PENDING_WRITES_KEY, payment_id, {**kept, "unconfirmed": True})


async def _clear_write(
    db: AsyncSession, invoice_id: uuid.UUID, payment_id: uuid.UUID, token: str | None
) -> None:
    """In the transaction that records a payment write's outcome: drop its
    pending-write record — only when it still names this attempt's token. A
    record re-claimed by another process after this claim lapsed is that
    process's to clear (decisions §584). Never commits."""
    if token is None:
        return
    instance = await _lock_instance(db, invoice_id)
    if instance is None:
        return
    record = _write_record(instance.state_data, payment_id)
    if record is not None and record.get("token") == token:
        _write_entry(instance, PENDING_WRITES_KEY, payment_id, None)


async def resume_pending_writes(org) -> int:
    """Finish every payment write whose outcome was never recorded (the
    outcome's transaction rolled back, or the process died around the ERP
    call — decisions §584). Called per org by the status-sync sweep BEFORE
    :func:`resume_pending_voids`, so a void re-armed here is voided in the
    same pass. Returns how many records are still there afterwards.

    Per record, once its claim has lapsed: a payment no longer ``completed``
    has its void re-armed (:func:`_rearm_void_if_voided`, with a fresh token)
    and the record dropped, in one transaction — whatever the write left in
    the ERP is voided by the void, which is a no-op when nothing stands. A
    payment still ``completed`` is written back again through
    :func:`write_back_payment`, which claims the record and whose adapter
    write finds our earlier BillPayment before it creates: recorded once,
    posted once."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine
    from app.models.workflow import WorkflowInstance

    found = await _org_adapter(org.id)
    if found is None:
        return 0
    _org, erp_config, adapter = found
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    left = 0
    async with factory() as db:
        rows = (
            await db.execute(
                select(WorkflowInstance.invoice_id, WorkflowInstance.state_data)
                .where(WorkflowInstance.state_data.has_key(PENDING_WRITES_KEY))
                .order_by(WorkflowInstance.invoice_id)
            )
        ).all()
        await db.commit()
        candidates = [
            (invoice_id, uuid.UUID(payment_id))
            for invoice_id, state in rows
            for payment_id in sorted((state or {}).get(PENDING_WRITES_KEY) or {})
        ]
        for invoice_id, payment_id in candidates:
            try:
                if await _resume_one_write(db, adapter, erp_config, invoice_id, payment_id):
                    left += 1
            except Exception as exc:  # noqa: BLE001 — one record never stops the rest
                await db.rollback()
                left += 1
                logger.warning(
                    "[payment-writeback] pending payment write retry failed for payment %s: %s",
                    payment_id,
                    exc.__class__.__name__,
                )
    return left


async def _resume_one_write(
    db: AsyncSession,
    adapter: ErpAdapter,
    erp_config: dict,
    invoice_id: uuid.UUID,
    payment_id: uuid.UUID,
) -> bool:
    """One pending payment write. Returns whether a record is still there."""
    from datetime import datetime

    instance = await _lock_instance(db, invoice_id)
    record = _write_record(instance.state_data if instance else None, payment_id)
    status, _run = await _payment_row(db, payment_id)
    if record is None or record.get("exhausted") or _claim_live(record, datetime.now(UTC)):
        await db.commit()
        return record is not None
    if status != "completed":
        # Voided (or gone) while the outcome went unrecorded: our BillPayment
        # may stand on its bill. Re-arm the void and drop the record together.
        if status is not None:
            await _rearm_void_if_voided(db, invoice_id, payment_id)
        _write_entry(instance, PENDING_WRITES_KEY, payment_id, None)
        await db.commit()
        return False
    current_bill = bill_for_writeback(instance.state_data, erp_config)
    if current_bill is None:
        # The connection no longer reaches the bill the record names (a
        # reconnect to another company): nothing can be written or found
        # under it. Kept, fail closed, as a pending void is in that case.
        await db.commit()
        return True
    if record.get("bill_id") is not None and str(record["bill_id"]) != str(current_bill):
        # The invoice points at another bill now (re-pushed, adopted another):
        # a retry would write against THAT bill, and the adapter's lookup for
        # our earlier payment would search the wrong one, so a payment that
        # stands on the recorded bill would be paid again on the new one.
        # Kept, fail closed, and flagged once for a person (decisions §588).
        invoice = await db.get(Invoice, invoice_id)
        if invoice is not None:
            await open_erp_reconciliation_exception(
                db,
                invoice,
                org_id=invoice.organization_id,
                finding=_write_failed_finding(adapter.erp_type, payment_id, unconfirmed=True),
                erp_report=_writeback_report(
                    ERP_REPORT_WRITEBACK_FAILED,
                    erp_type=adapter.erp_type,
                    payment_id=payment_id,
                    payment_run_id=_run,
                    bill_id=str(record["bill_id"]),
                    failure=WRITE_BILL_CHANGED,
                    current_bill_id=str(current_bill),
                    attempts=record.get("attempts"),
                    outcome=WRITE_UNCONFIRMED,
                ),
                dedupe_on=("outcome", "failure"),
            )
        await db.commit()
        return True
    state = dict(instance.state_data or {})
    await db.commit()
    payment = await db.get(Payment, payment_id)
    invoice = await db.get(Invoice, invoice_id)
    if payment is None or invoice is None:
        await db.commit()
        return True
    await write_back_payment(
        db,
        adapter=adapter,
        erp_config=erp_config,
        payment=payment,
        invoice=invoice,
        state_data=state,
    )
    still = _write_record(await _state_data(db, invoice_id), payment_id) is not None
    await db.commit()
    return still


async def rearm_capped_on_resolve(db: AsyncSession, exception) -> dict | None:
    """A person resolved a capped write-back or void flag: clear the cap so
    the sweep tries again (decisions §588).

    Called by ``exception_lifecycle.record_decision`` for a person's
    ``resolve`` (never an agent's, never ``dismiss``: dismissing says the
    person has taken the ERP side over, and the record stays capped), in the
    decision's transaction. Only a row whose ``erp_report`` is a
    :data:`RETRIES_EXHAUSTED` write or void flag, and only while that record
    is still ``exhausted``. A write's attempts start again: its adapter write
    finds our earlier payment by its marker before it creates, so a retry
    records what stands and never posts twice. A void's attempts entry is
    dropped while the payment is still pending its void. Returns the PII-free
    detail the decision's audit row carries, or None when nothing was capped.
    Never commits."""
    report = getattr(exception, "erp_report", None)
    if not isinstance(report, dict) or report.get("failure") != RETRIES_EXHAUSTED:
        return None
    kind = report.get("kind")
    if kind not in (ERP_REPORT_WRITEBACK_FAILED, ERP_REPORT_VOID_WRITEBACK_FAILED):
        return None
    invoice_id = getattr(exception, "invoice_id", None)
    try:
        payment_id = uuid.UUID(str(report.get("payment_id")))
    except ValueError:
        return None
    if invoice_id is None:
        return None
    instance = await _lock_instance(db, invoice_id)
    if instance is None:
        return None
    state = instance.state_data or {}
    if kind == ERP_REPORT_WRITEBACK_FAILED:
        record = _write_record(state, payment_id)
        if record is None or not record.get("exhausted"):
            return None
        kept = {
            k: v
            for k, v in record.items()
            if k not in ("exhausted", "claimed_until", "token", "attempts")
        }
        _write_entry(instance, PENDING_WRITES_KEY, payment_id, {**kept, "attempts": 0})
        return {"erp_retry_rearmed": "payment_write", "payment_id": str(payment_id)}
    attempts = (state.get(VOID_ATTEMPTS_KEY) or {}).get(str(payment_id)) or {}
    pending = [str(p) for p in state.get(PENDING_VOIDS_KEY) or []]
    if not attempts.get("exhausted") or str(payment_id) not in pending:
        return None
    _write_entry(instance, VOID_ATTEMPTS_KEY, payment_id, None)
    return {"erp_retry_rearmed": "payment_void", "payment_id": str(payment_id)}


async def _call_credit(adapter: ErpAdapter, payload: DiscountCreditPayload) -> ErpPostResult:
    try:
        return await adapter.post_discount_credit(payload)
    except Exception as exc:  # noqa: BLE001 — an adapter bug is a failed credit
        return ErpPostResult(
            success=False,
            erp_document_id=payload.credit_id,
            message=f"discount credit error: {exc.__class__.__name__}",
        )


async def _call_recorded_credit(
    adapter: ErpAdapter, payload: DiscountCreditPayload, *, completed: bool
) -> tuple[ErpPostResult, bool]:
    """One attempt at a RECORDED credit, which never creates one: with no
    known id the ERP is asked for ours by a lookup that can only say "found"
    on a positive match (QuickBooks: through the bill's applying BillPayment;
    Xero: by our ``CreditNoteNumber``, checked on our side). Not found is NOT
    proof that none stands — the create's answer was lost — so the record is
    held and a person told (``held`` True), never re-created on a provider's
    unconfirmed request-key replay (decisions §579). For a payment no longer
    ``completed`` the lookup only finds the id, for its void."""
    if not payload.credit_id:
        try:
            found = await adapter.find_discount_credit(payload)
        except Exception as exc:  # noqa: BLE001 — unreadable: try again next pass
            return (
                ErpPostResult(
                    success=False,
                    message=f"discount credit lookup failed: {exc.__class__.__name__}",
                ),
                False,
            )
        if not found:
            return (
                ErpPostResult(
                    success=False,
                    message=f"{adapter.erp_type} post failed: {DISCOUNT_CREDIT_UNIDENTIFIED}",
                ),
                True,
            )
        payload.credit_id = str(found)
    if not completed:
        return (
            ErpPostResult(
                success=False,
                erp_document_id=payload.credit_id,
                message=f"{adapter.erp_type} post failed: payment_not_completed",
            ),
            False,
        )
    return await _call_credit(adapter, payload), False


async def _record_credit_outcome(
    db: AsyncSession,
    adapter: ErpAdapter,
    invoice: Invoice,
    payment_id: uuid.UUID,
    payload: DiscountCreditPayload,
    credit: ErpPostResult,
    *,
    prior: dict | None,
    held: bool,
    reserved: bool = False,
    payment_run_id: uuid.UUID | None = None,
) -> dict | None:
    """Phase two of a credit attempt, under the instance's row lock: record
    what the attempt left, audit it when its outcome changed, and flag what a
    person must finish. Never commits. Returns the record kept (None = none).

    * The payment is re-read under the lock: a void that committed while the
      ERP was called has already run (or is about to run) its reversal without
      this credit, so a credit id now held is written to
      :data:`PENDING_VOID_CREDITS_KEY` and the payment's void re-armed in
      :data:`PENDING_VOIDS_KEY` — the credit is reversed, never left applied
      to the bill of a voided payment (decisions §579).
    * The record is dropped only on a confirmed outcome: the credit applied,
      or ours refused ``bill_payment_voided`` (someone decided in the ERP). A
      refusal before any write (``not_posted``) keeps an earlier record: the
      credit that attempt recorded still stands.
    * A record with no known id, or one at :data:`MAX_CREDIT_ATTEMPTS`, opens
      the ``discount_credit_stalled`` flag (deduped by kind).
    * ``reserved``: this attempt was the create over its own reservation
      (:func:`_reserve_credit`), which is not an earlier credit — a refusal
      before any write drops it (decisions §581).
    """
    from datetime import datetime

    from app.services.erp_adapters.base import BILL_PAYMENT_VOIDED

    now = datetime.now(UTC)
    instance = await _lock_instance(db, invoice.id)
    if instance is None:
        return None
    base = _pending_record(instance.state_data, payment_id) or prior
    status, run_id = await _payment_row(db, payment_id)
    run_id = run_id or payment_run_id
    gone = credit.refusal_code == BILL_PAYMENT_VOIDED
    credit_id = credit.erp_document_id or payload.credit_id or (base or {}).get("credit_id")
    earlier = base if not (reserved and not (base or {}).get("credit_id")) else None
    may_stand = not gone and (credit.success or not credit.not_posted or earlier is not None)
    rearmed = False
    if status != "completed" and may_stand and credit_id:
        voids = [str(p) for p in (instance.state_data or {}).get(PENDING_VOIDS_KEY) or []]
        if str(payment_id) not in voids:
            voids.append(str(payment_id))
        instance.state_data = {**(instance.state_data or {}), PENDING_VOIDS_KEY: voids}
        _write_entry(instance, PENDING_VOID_CREDITS_KEY, payment_id, str(credit_id))
        kept = None
        rearmed = True
        outcome = "void_rearmed"
    elif credit.success and status == "completed":
        kept, outcome = None, "posted"
        if credit_id:
            # Remembered for the void, which reverses it by this id (§582).
            _write_entry(instance, BOOKED_CREDITS_KEY, payment_id, str(credit_id))
    elif gone or not may_stand:
        kept = None
        outcome = credit.refusal_code or "not_posted"
    else:
        attempts = int((base or {}).get("attempts") or 0) + 1
        if held:
            outcome = DISCOUNT_CREDIT_UNIDENTIFIED
        else:
            outcome = credit.refusal_code or credit.message or "failed"
        kept = {
            **_credit_marker(payload, credit_id),
            "attempts": attempts,
            "last_attempt": now.isoformat(),
            "outcome": outcome,
            **({"exhausted": True} if attempts >= MAX_CREDIT_ATTEMPTS else {}),
        }
    _write_entry(instance, PENDING_CREDITS_KEY, payment_id, kept)

    newly_exhausted = bool(kept and kept.get("exhausted") and not (base or {}).get("exhausted"))
    changed = base is None or base.get("outcome") != outcome or newly_exhausted
    if credit.success or changed:
        refusal = _refusal(credit)
        await dispatch_audit(
            db,
            correlation_id=invoice.correlation_id or invoice.id,
            organization_id=invoice.organization_id,
            actor_id=None,
            action=(
                "payment.erp_discount_credit_posted"
                if credit.success
                else "payment.erp_discount_credit_failed"
            ),
            entity_type="payment",
            entity_id=payment_id,
            details={
                "erp_type": adapter.erp_type,
                "bill_id": payload.bill_id,
                "erp_document_id": credit_id,
                "discount_amount": str(payload.amount),
                "currency": payload.currency,
                **({"retry": True} if prior is not None else {}),
                **({} if credit.success else {"error": credit.message}),
                **({"refusal": refusal} if refusal else {}),
                **({"attempts": kept["attempts"]} if kept else {}),
                **({"retries_exhausted": True} if newly_exhausted else {}),
                **({"void_rearmed": True} if rearmed else {}),
            },
        )
    not_ours = (credit.message or "").endswith(f": {DISCOUNT_CREDIT_NOT_OURS}")
    if kept is not None and (kept.get("credit_id") is None or kept.get("exhausted") or not_ours):
        if kept.get("exhausted"):
            reason = "retries_exhausted"
        elif not_ours:
            reason = DISCOUNT_CREDIT_NOT_OURS
        else:
            reason = "unidentified"
        await _open_stalled_flag(
            db,
            adapter,
            invoice,
            payment_id,
            run_id,
            payload.bill_id,
            kept.get("credit_id"),
            reason=reason,
            attempts=kept.get("attempts"),
        )
    return kept


async def _open_stalled_flag(
    db: AsyncSession,
    adapter: ErpAdapter,
    invoice: Invoice,
    payment_id: uuid.UUID,
    run_id: uuid.UUID | None,
    bill_id: str,
    credit_id: str | None,
    *,
    reason: str,
    attempts: int | None = None,
) -> None:
    """The person-facing flag for a credit FeohLedger will not finish alone.
    Not payment-blocking (``erp_reconciliation``): the payment it belongs to
    has already moved, and a stray credit is an ERP bookkeeping fix — holding
    a re-pay of the bill for it would delay the supplier for nothing the
    re-pay's own write-back does not already check (decisions §579)."""
    await open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=invoice.organization_id,
        finding=erp_finding("erp_discount_credit_stalled", adapter.erp_type),
        erp_report=_writeback_report(
            ERP_REPORT_DISCOUNT_CREDIT_STALLED,
            erp_type=adapter.erp_type,
            payment_id=payment_id,
            payment_run_id=run_id,
            bill_id=bill_id,
            reason=reason,
            # What to look for in the ERP: QuickBooks' PrivateNote, Xero's
            # CreditNoteNumber (decisions §575).
            discount_credit_marker=f"FeohLedger discount {payment_id}",
            discount_credit_document_id=credit_id,
            attempts=attempts,
        ),
        # One live flag per payment and reason: an open "unidentified" flag
        # for one payment never hides a later "not ours" with its id, nor
        # another payment's stall on the same invoice (decisions §583).
        dedupe_on=("payment_id", "reason"),
    )


async def resume_pending_credits(org) -> int:
    """Finish every discount credit a write-back left standing unapplied
    (decisions §577, §579). Called per org by the status-sync sweep. Returns
    how many are still recorded afterwards.

    Two-phase, per record (``docs/background-sweeps.md`` § Locking): the
    candidate records are read unlocked; each is then claimed under the
    instance's row lock (:func:`_claim_record`), the ERP is called with no
    lock held, and the outcome is written under the lock again
    (:func:`_record_credit_outcome`), which re-reads the payment. A recorded
    credit is never created again: one with a known id is applied by that id
    (one deleted or no longer ours is dropped, audited), one with none is
    looked up and otherwise held for a person. A payment no longer
    ``completed`` is left to its void when the id is known."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine
    from app.models.workflow import WorkflowInstance

    found = await _org_adapter(org.id)
    if found is None:
        return 0
    _org, _erp_config, adapter = found
    if getattr(adapter, "supports_discount_credit", False) is not True:
        return 0
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    left = 0
    async with factory() as db:
        rows = (
            await db.execute(
                select(WorkflowInstance.invoice_id, WorkflowInstance.state_data)
                .where(WorkflowInstance.state_data.has_key(PENDING_CREDITS_KEY))
                .order_by(WorkflowInstance.invoice_id)
            )
        ).all()
        await db.commit()
        candidates = [
            (invoice_id, uuid.UUID(payment_id))
            for invoice_id, state in rows
            for payment_id in sorted((state or {}).get(PENDING_CREDITS_KEY) or {})
        ]
        for invoice_id, payment_id in candidates:
            try:
                if await _resume_one_credit(db, adapter, invoice_id, payment_id):
                    left += 1
            except Exception as exc:  # noqa: BLE001 — one record never stops the rest
                await db.rollback()
                left += 1
                logger.warning(
                    "[payment-writeback] discount credit retry failed for payment %s: %s",
                    payment_id,
                    exc.__class__.__name__,
                )
    return left


async def _resume_one_credit(
    db: AsyncSession, adapter: ErpAdapter, invoice_id: uuid.UUID, payment_id: uuid.UUID
) -> bool:
    """One recorded credit, claimed, attempted and recorded. Returns whether a
    record is still there afterwards."""
    record, status = await _claim_record(db, invoice_id, payment_id)
    if record is None:
        state = await _state_data(db, invoice_id)
        await db.commit()
        return _pending_record(state, payment_id) is not None
    payload = _payload_from_marker(str(payment_id), record)
    credit, held = await _call_recorded_credit(adapter, payload, completed=status == "completed")
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None:
        await db.commit()
        return False
    kept = await _record_credit_outcome(
        db, adapter, invoice, payment_id, payload, credit, prior=record, held=held
    )
    await db.commit()
    return kept is not None


async def record_pending_erp_void(db: AsyncSession, *, invoice: Invoice, payment: Payment) -> bool:
    """In ``POST /api/payments/{id}/void``'s transaction: note that this
    payment's write-back (if any) must be voided in the ERP. Only a FeohLedger
    payment of an invoice that was posted to the ERP qualifies; returns whether
    one was recorded. Never commits."""

    from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER

    if payment.provider == EXTERNAL_PAYMENT_PROVIDER:
        return False  # never written back (payment_erp_sync skips them)
    # Under the instance's row lock, read fresh: a discount-credit attempt
    # records its outcome under the same lock and re-reads the payment there,
    # so either it sees this void (and re-arms it with the credit it holds) or
    # this void sees its record — never a lost write between them (§579).
    instance = await _lock_instance(db, invoice.id)
    state = dict((instance.state_data if instance else None) or {})
    # A mock bill is never written back (``bill_for_writeback`` reads the
    # connection realm, which the mock lacks), so it has nothing to void and a
    # marker would only stay pending for ever (§538).
    recorded = state.get("erp_realm")
    if instance is None or not state.get("erp_reference"):
        return False
    if recorded in (None, "", MOCK_BILL_REALM):
        return False
    pending = [str(p) for p in state.get(PENDING_VOIDS_KEY) or []]
    if str(payment.id) not in pending:
        pending.append(str(payment.id))
    instance.state_data = {**state, PENDING_VOIDS_KEY: pending}
    return True


async def _clear_pending(db: AsyncSession, invoice_id: uuid.UUID, payment_id: uuid.UUID) -> None:
    from sqlalchemy import select

    from app.models.workflow import WorkflowInstance

    instance = (
        await db.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.invoice_id == invoice_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if instance is None:
        return
    state = dict(instance.state_data or {})
    pending = [p for p in state.get(PENDING_VOIDS_KEY) or [] if p != str(payment_id)]
    if pending:
        state[PENDING_VOIDS_KEY] = pending
    else:
        state.pop(PENDING_VOIDS_KEY, None)
    instance.state_data = state


def dispatch_void_writeback(org_id: uuid.UUID, payment_id: uuid.UUID) -> None:
    """After ``POST /api/payments/{id}/void`` commits, void the ERP payment we
    wrote for it, on the request's loop and off the response."""
    _spawn(void_written_payment(org_id, payment_id), f"void-writeback-{payment_id}")


async def void_written_payment(org_id: uuid.UUID, payment_id: uuid.UUID) -> str:
    """Void the ERP payment written back for ``payment_id``. Never raises.

    A void here that leaves our BillPayment standing in the ERP would show the
    bill paid there while FeohLedger owes it again: the bookkeeper would not
    pay it, and the books would disagree. So a failure opens an
    ``erp_reconciliation`` exception naming the cause and leaves the pending
    marker for the sweep to retry; a success writes ``payment.erp_voided`` —
    or ``payment.erp_void_not_needed`` when nothing of ours was there — and
    clears the marker.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine

    try:
        found = await _org_adapter(org_id)
        if found is None:
            return SKIPPED
        org, erp_config, adapter = found
        factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
        async with factory() as db:
            payment = await db.get(Payment, payment_id)
            if payment is None:
                return SKIPPED
            invoice = await db.get(Invoice, payment.invoice_id)
            state = await _state_data(db, payment.invoice_id)
            bill_id = bill_for_writeback(state, erp_config)
            if invoice is None or bill_id is None:
                return SKIPPED
            payload = PaymentPayload(
                payment_id=str(payment.id),
                bill_id=bill_id,
                amount=Decimal(payment.amount),
                currency=(invoice.currency or "").upper(),
                paid_on=utc_today(),
            )
            correlation = invoice.correlation_id or payment.correlation_id or invoice.id
            invoice_id = invoice.id
            run_id = payment.payment_run_id
            # What an earlier attempt recorded of our discount credit (§577):
            # the id a previous void stored before deleting anything, or the
            # one an unapplied credit's write-back stored.
            known_credit = (
                ((state or {}).get(PENDING_VOID_CREDITS_KEY) or {}).get(str(payment_id))
                or (((state or {}).get(PENDING_CREDITS_KEY) or {}).get(str(payment_id)) or {}).get(
                    "credit_id"
                )
                # The credit a write-back booked, by id (§582): never left to
                # Xero's unconfirmed lookup by number.
                or ((state or {}).get(BOOKED_CREDITS_KEY) or {}).get(str(payment_id))
            )
            # Since §581 every credit we create is preceded by a committed
            # reservation, so a credit of ours always has an entry in one of
            # these maps. Without one there is nothing of ours to reverse, and
            # no lookup is run: Xero's is by an unconfirmed number filter, and
            # a rejection there would stop every void before ``void_payment``
            # for an org that never opted in (decisions §583).
            has_credit_entry = any(
                str(payment_id) in ((state or {}).get(key) or {})
                for key in (PENDING_CREDITS_KEY, PENDING_VOID_CREDITS_KEY, BOOKED_CREDITS_KEY)
            )
            # A write-back that re-arms this void while it calls the ERP
            # changes this token; the clear below compares it (§582).
            rearm_token = ((state or {}).get(VOID_REARMED_KEY) or {}).get(str(payment_id))
            # Likewise the booked id: one written while this void calls the
            # ERP is a credit this pass may not have reversed (§583).
            booked_then = ((state or {}).get(BOOKED_CREDITS_KEY) or {}).get(str(payment_id))
            # A void that failed :data:`MAX_VOID_ATTEMPTS` times is a
            # person's (its flag and audit say so): no further ERP call until
            # a write-back re-arms it (decisions §584).
            if (((state or {}).get(VOID_ATTEMPTS_KEY) or {}).get(str(payment_id)) or {}).get(
                "exhausted"
            ):
                await db.rollback()
                return FAILED
            # Nothing held across the ERP call. A rollback expires the loaded
            # rows, so what is needed after it was read above.
            await db.rollback()
            # Our discount credit first (§575), whatever the opt-in says now:
            # it may have been booked while it was on — its entry above says
            # so. A failure stops here, the marker stays, and the retry starts
            # from the credit again.
            credit = None
            credit_id = known_credit
            if has_credit_entry and getattr(adapter, "supports_discount_credit", False) is True:
                credit_payload = DiscountCreditPayload(
                    payment_id=str(payment_id),
                    bill_id=bill_id,
                    amount=Decimal(0),
                    currency=payload.currency,
                    credit_on=payload.paid_on,
                    credit_id=known_credit,
                )
                try:
                    if credit_id is None:
                        credit_id = await adapter.find_discount_credit(credit_payload)
                except Exception as exc:  # noqa: BLE001 — unreadable: stop, retry later
                    credit = ErpPostResult(
                        success=False,
                        message=f"discount credit lookup failed: {exc.__class__.__name__}",
                    )
                if credit is None:
                    if credit_id and credit_id != known_credit:
                        # Recorded BEFORE anything is deleted: once the
                        # application goes, the bill no longer leads to it.
                        await _set_map_entry(
                            db, invoice_id, PENDING_VOID_CREDITS_KEY, payment_id, str(credit_id)
                        )
                        await db.commit()
                    credit_payload.credit_id = credit_id
                    credit = await adapter.void_discount_credit(credit_payload)
                    if not credit.success and not credit.erp_document_id:
                        credit.erp_document_id = credit_id
            # A recorded credit that reads back without our marker was edited
            # in the ERP: it is no longer ours to reverse, and holding the
            # payment's void behind it would leave our whole BillPayment on a
            # voided payment's bill where only the credit is in doubt. For
            # this one refusal the payment is voided anyway and the credit
            # stays recorded and flagged on its own (decisions §584, a
            # narrowing of §575's credit-first order).
            credit_not_ours = (
                credit is not None
                and not credit.success
                and (credit.message or "").endswith(f": {DISCOUNT_CREDIT_NOT_OURS}")
            )
            if credit_not_ours:
                credit_id = credit.erp_document_id or credit_id
            if credit is not None and not credit.success and not credit_not_ours:
                result = credit
            else:
                result = await adapter.void_payment(payload)
            invoice = await db.get(Invoice, invoice_id)
            if credit is not None and credit.success and credit.erp_document_id:
                await dispatch_audit(
                    db,
                    correlation_id=correlation,
                    organization_id=invoice.organization_id,
                    actor_id=None,
                    action="payment.erp_discount_credit_reversed",
                    entity_type="payment",
                    entity_id=payment_id,
                    details={
                        "erp_type": adapter.erp_type,
                        "bill_id": bill_id,
                        "erp_document_id": credit.erp_document_id,
                    },
                )
            refusal = _refusal(result)
            on_credit = credit is not None and result is credit
            held = {"discount_credit_held": str(credit_id)} if credit_not_ours else {}
            unidentified = False
            if result.success:
                voided = bool(result.erp_document_id)
                await dispatch_audit(
                    db,
                    correlation_id=correlation,
                    organization_id=invoice.organization_id,
                    actor_id=None,
                    action="payment.erp_voided" if voided else "payment.erp_void_not_needed",
                    entity_type="payment",
                    entity_id=payment_id,
                    details={
                        "erp_type": adapter.erp_type,
                        "bill_id": bill_id,
                        "erp_document_id": result.erp_document_id,
                        **held,
                    },
                )
                # Compare, then clear, under the lock: a credit attempt that
                # ran while this void was calling the ERP may have re-armed it
                # with a credit this pass never saw — that one stays pending
                # for the next pass. A recorded credit with no known id that
                # nothing found may still stand: it stays recorded and flagged,
                # and the void does not report success over it (§579).
                instance = await _lock_instance(db, invoice_id)
                current = dict((instance.state_data if instance else None) or {})
                rearmed = (current.get(PENDING_VOID_CREDITS_KEY) or {}).get(str(payment_id))
                record_now = _pending_record(current, payment_id)
                # A payment write-back that landed after our ERP call and
                # re-armed this void (its BillPayment may stand) leaves it
                # pending for the next pass (§582).
                token_now = (current.get(VOID_REARMED_KEY) or {}).get(str(payment_id))
                booked_now = (current.get(BOOKED_CREDITS_KEY) or {}).get(str(payment_id))
                finished = (
                    instance is not None
                    and not (rearmed and rearmed != credit_id)
                    and token_now == rearm_token
                    and booked_now == booked_then
                )
                if finished:
                    await _clear_pending(db, invoice_id, payment_id)
                    _write_entry(instance, VOID_REARMED_KEY, payment_id, None)
                    _write_entry(instance, VOID_ATTEMPTS_KEY, payment_id, None)
                # A credit held as not ours keeps every entry naming it: it
                # stays recorded, beside its flag (decisions §584).
                if finished and not credit_not_ours:
                    _write_entry(instance, PENDING_VOID_CREDITS_KEY, payment_id, None)
                    if credit is not None and booked_now == credit_id:
                        # Compare, then clear: only the id this void reversed
                        # (or confirmed gone). Any other booked id is a credit
                        # this pass did not reverse, and its id is kept
                        # (decisions §583).
                        _write_entry(instance, BOOKED_CREDITS_KEY, payment_id, None)
                    unidentified = (
                        credit is not None
                        and not credit_id
                        and record_now is not None
                        and not record_now.get("credit_id")
                    )
                    if not unidentified:
                        _write_entry(instance, PENDING_CREDITS_KEY, payment_id, None)
                if unidentified:
                    await _open_stalled_flag(
                        db,
                        adapter,
                        invoice,
                        payment_id,
                        run_id,
                        bill_id,
                        None,
                        reason="unidentified",
                        attempts=record_now.get("attempts"),
                    )
            else:
                # Audited when the outcome changes, not every pass; capped
                # like a recorded credit (decisions §584, after §579).
                from datetime import datetime

                instance = await _lock_instance(db, invoice_id)
                prior = (
                    ((instance.state_data if instance else None) or {}).get(VOID_ATTEMPTS_KEY) or {}
                ).get(str(payment_id)) or {}
                outcome = (
                    f"{'discount_credit' if on_credit else 'payment'}:"
                    f"{refusal['code'] if refusal else (result.message or 'failed')}"
                )
                attempts = int(prior.get("attempts") or 0) + 1
                exhausted = attempts >= MAX_VOID_ATTEMPTS
                newly_exhausted = exhausted and not prior.get("exhausted")
                if instance is not None:
                    _write_entry(
                        instance,
                        VOID_ATTEMPTS_KEY,
                        payment_id,
                        {
                            "outcome": outcome,
                            "attempts": attempts,
                            "last_attempt": datetime.now(UTC).isoformat(),
                            **({"exhausted": True} if exhausted else {}),
                        },
                    )
                if prior.get("outcome") != outcome or newly_exhausted:
                    await dispatch_audit(
                        db,
                        correlation_id=correlation,
                        organization_id=invoice.organization_id,
                        actor_id=None,
                        action="payment.erp_void_failed",
                        entity_type="payment",
                        entity_id=payment_id,
                        details={
                            "erp_type": adapter.erp_type,
                            "bill_id": bill_id,
                            "erp_document_id": result.erp_document_id,
                            "error": result.message,
                            "attempts": attempts,
                            **({"refusal": refusal} if refusal else {}),
                            **({"document": "discount_credit"} if on_credit else {}),
                            **({"retries_exhausted": True} if newly_exhausted else {}),
                            **held,
                        },
                    )
                await open_erp_reconciliation_exception(
                    db,
                    invoice,
                    org_id=invoice.organization_id,
                    finding=erp_finding("erp_payment_void_writeback_failed", adapter.erp_type),
                    erp_report=_writeback_report(
                        ERP_REPORT_VOID_WRITEBACK_FAILED,
                        erp_type=adapter.erp_type,
                        payment_id=payment_id,
                        payment_run_id=run_id,
                        bill_id=bill_id,
                        # At the cap the row says so, and opens on its own
                        # (below): nothing calls the ERP again until a person
                        # resolves it (decisions §588).
                        failure=(
                            RETRIES_EXHAUSTED
                            if exhausted
                            else (refusal["code"] if refusal else "erp_void_failed")
                        ),
                        refusal=refusal,
                        retryable=result.retryable,
                        attempts=attempts,
                        # Our credit, when the reversal stopped on it: with
                        # its application deleted, only this id leads to it.
                        **(
                            {
                                "document": "discount_credit",
                                "discount_credit_document_id": credit.erp_document_id,
                            }
                            if credit is not None and result is credit
                            else {}
                        ),
                    ),
                    dedupe_on=("failure",) if exhausted else (),
                )
            if credit_not_ours:
                await _open_stalled_flag(
                    db,
                    adapter,
                    invoice,
                    payment_id,
                    run_id,
                    bill_id,
                    str(credit_id),
                    reason=DISCOUNT_CREDIT_NOT_OURS,
                )
            await db.commit()
            # A held credit does not fail the payment's void: that is done, and
            # the credit has its own flag (decisions §584).
            return WROTE if result.success and not unidentified else FAILED
    except Exception as exc:  # noqa: BLE001 — best-effort, after the void committed
        logger.warning(
            "[payment-writeback] void write-back failed for payment %s: %s",
            payment_id,
            exc.__class__.__name__,
        )
        return FAILED


async def resume_pending_voids(org) -> int:
    """Finish every ERP void a payment void recorded and nothing completed
    (the process died, or the ERP refused). Called per org by the status-sync
    sweep. Returns how many are still pending afterwards."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine
    from app.models.workflow import WorkflowInstance

    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    async with factory() as db:
        rows = (
            await db.execute(
                select(WorkflowInstance.state_data).where(
                    WorkflowInstance.state_data.has_key(PENDING_VOIDS_KEY)
                )
            )
        ).scalars()
        pending = [p for state in rows for p in (state or {}).get(PENDING_VOIDS_KEY) or []]
    left = 0
    for payment_id in pending:
        if await void_written_payment(org.id, uuid.UUID(payment_id)) == FAILED:
            left += 1
    return left
