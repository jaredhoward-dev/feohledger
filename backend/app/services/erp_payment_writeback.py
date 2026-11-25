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
  books it. A void reverses our credit before it voids our payment.
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
    credit_payload: DiscountCreditPayload | None = None
    credit: ErpPostResult | None = None
    try:
        payload = await build_payment_payload(
            db, payment=payment, invoice=invoice, bill_id=bill_id, erp_config=erp_config
        )
        if getattr(adapter, "supports_discount_credit", False) is True:
            credit_payload = await build_discount_credit_payload(
                db,
                payment=payment,
                invoice=invoice,
                payment_payload=payload,
                erp_config=erp_config,
            )
        # End the read transaction: no connection is held open across the ERP
        # call (the caller's session does not expire rows on commit).
        await db.commit()
        result = await adapter.post_payment(payload)
    except Exception as exc:  # noqa: BLE001 — an adapter bug is a failed write-back
        result = ErpPostResult(success=False, message=f"write-back error: {exc.__class__.__name__}")
    if result.success and credit_payload is not None:
        # Only after our payment stands on the bill: the credit is capped by
        # what the bill still has open AFTER it (the adapter refuses more).
        try:
            credit = await adapter.post_discount_credit(credit_payload)
        except Exception as exc:  # noqa: BLE001 — an adapter bug is a failed credit
            credit = ErpPostResult(
                success=False, message=f"discount credit error: {exc.__class__.__name__}"
            )

    try:
        if result.success:
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
                },
            )
            balance_after = result.bill_balance_after
            credit_refusal = None
            if credit is not None:
                credit_refusal = _refusal(credit)
                await dispatch_audit(
                    db,
                    correlation_id=correlation,
                    organization_id=invoice.organization_id,
                    actor_id=None,
                    action=(
                        "payment.erp_discount_credit_posted"
                        if credit.success
                        else "payment.erp_discount_credit_failed"
                    ),
                    entity_type="payment",
                    entity_id=payment.id,
                    details={
                        "erp_type": adapter.erp_type,
                        "bill_id": bill_id,
                        "erp_document_id": credit.erp_document_id,
                        "discount_amount": str(credit_payload.amount),
                        "currency": credit_payload.currency,
                        **({} if credit.success else {"error": credit.message}),
                        **({"refusal": credit_refusal} if credit_refusal else {}),
                    },
                )
                if credit.success and credit.bill_balance_after is not None:
                    balance_after = credit.bill_balance_after
                # A credit that may stand in the ERP (created, its application
                # failed or unconfirmed) is recorded so the sweep applies THAT
                # one and nobody books a second (decisions §577).
                credit_pending = not credit.success and not credit.not_posted
                await _set_pending_credit(
                    db,
                    invoice.id,
                    payment.id,
                    _credit_marker(credit_payload, credit.erp_document_id)
                    if credit_pending
                    else None,
                )
            if balance_after is not None and balance_after > 0:
                await open_erp_reconciliation_exception(
                    db,
                    invoice,
                    org_id=invoice.organization_id,
                    finding=erp_finding(
                        "erp_payment_writeback_balance_open",
                        adapter.erp_type,
                        amount=balance_after,
                        currency=invoice.currency,
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
                                "discount_credit_document_id": credit.erp_document_id,
                            }
                            if credit is not None and not credit.success and not credit.not_posted
                            else {}
                        ),
                    ),
                )
            await db.commit()
            return WROTE

        refusal = _refusal(result)
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
            finding=erp_finding("erp_payment_writeback_failed", adapter.erp_type),
            erp_report=_writeback_report(
                ERP_REPORT_WRITEBACK_FAILED,
                erp_type=adapter.erp_type,
                payment_id=payment.id,
                payment_run_id=payment.payment_run_id,
                bill_id=bill_id,
                failure=refusal["code"] if refusal else "erp_post_failed",
                refusal=refusal,
                retryable=result.retryable,
            ),
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — recording must never fail the leg
        await db.rollback()
        logger.warning(
            "[payment-writeback] could not record the outcome for payment %s: %s",
            payment.id,
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


async def _set_map_entry(
    db: AsyncSession, invoice_id: uuid.UUID, key: str, payment_id: uuid.UUID, value
) -> None:
    """Set (or, with ``value`` None, drop) ``state_data[key][payment_id]``
    under the instance's row lock. Never commits."""
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


async def _set_pending_credit(
    db: AsyncSession, invoice_id: uuid.UUID, payment_id: uuid.UUID, marker: dict | None
) -> None:
    await _set_map_entry(db, invoice_id, PENDING_CREDITS_KEY, payment_id, marker)


async def resume_pending_credits(org) -> int:
    """Apply every discount credit a write-back left standing unapplied
    (decisions §577), through the adapter's own path with the recorded credit
    id. Called per org by the status-sync sweep. Returns how many are still
    pending afterwards. A payment no longer ``completed`` is left to its void,
    which reverses the credit; a credit someone deleted in the ERP is dropped
    with an audited failure, never re-created."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import get_tenant_engine
    from app.models.workflow import WorkflowInstance
    from app.services.erp_adapters.base import BILL_PAYMENT_VOIDED

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
                select(WorkflowInstance.invoice_id, WorkflowInstance.state_data).where(
                    WorkflowInstance.state_data.has_key(PENDING_CREDITS_KEY)
                )
            )
        ).all()
        await db.rollback()
        for invoice_id, state in rows:
            for payment_id, marker in dict((state or {}).get(PENDING_CREDITS_KEY) or {}).items():
                status = (
                    await db.execute(
                        select(Payment.status).where(Payment.id == uuid.UUID(payment_id))
                    )
                ).scalar_one_or_none()
                await db.rollback()
                if status != "completed":
                    continue  # its void reverses the credit (by this id)
                payload = _payload_from_marker(payment_id, marker)
                try:
                    credit = await adapter.post_discount_credit(payload)
                except Exception as exc:  # noqa: BLE001 — an adapter bug is a failed retry
                    credit = ErpPostResult(
                        success=False, message=f"discount credit error: {exc.__class__.__name__}"
                    )
                invoice = await db.get(Invoice, invoice_id)
                refusal = _refusal(credit)
                gone = credit.refusal_code == BILL_PAYMENT_VOIDED
                if credit.success or gone:
                    await _set_pending_credit(db, invoice_id, uuid.UUID(payment_id), None)
                elif credit.erp_document_id and credit.erp_document_id != marker.get("credit_id"):
                    await _set_pending_credit(
                        db,
                        invoice_id,
                        uuid.UUID(payment_id),
                        {**marker, "credit_id": credit.erp_document_id},
                    )
                if invoice is not None:
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
                        entity_id=uuid.UUID(payment_id),
                        details={
                            "erp_type": adapter.erp_type,
                            "bill_id": payload.bill_id,
                            "erp_document_id": credit.erp_document_id or payload.credit_id,
                            "discount_amount": str(payload.amount),
                            "currency": payload.currency,
                            "retry": True,
                            **({} if credit.success else {"error": credit.message}),
                            **({"refusal": refusal} if refusal else {}),
                        },
                    )
                await db.commit()
                if not (credit.success or gone):
                    left += 1
    return left


async def record_pending_erp_void(db: AsyncSession, *, invoice: Invoice, payment: Payment) -> bool:
    """In ``POST /api/payments/{id}/void``'s transaction: note that this
    payment's write-back (if any) must be voided in the ERP. Only a FeohLedger
    payment of an invoice that was posted to the ERP qualifies; returns whether
    one was recorded. Never commits."""
    from sqlalchemy import select

    from app.models.workflow import WorkflowInstance
    from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER

    if payment.provider == EXTERNAL_PAYMENT_PROVIDER:
        return False  # never written back (payment_erp_sync skips them)
    instance = (
        await db.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == invoice.id))
    ).scalar_one_or_none()
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
            known_credit = ((state or {}).get(PENDING_VOID_CREDITS_KEY) or {}).get(
                str(payment_id)
            ) or (((state or {}).get(PENDING_CREDITS_KEY) or {}).get(str(payment_id)) or {}).get(
                "credit_id"
            )
            # Nothing held across the ERP call. A rollback expires the loaded
            # rows, so what is needed after it was read above.
            await db.rollback()
            # Our discount credit first (§575), whatever the opt-in says now:
            # it may have been booked while it was on. A failure stops here,
            # the marker stays, and the retry starts from the credit again.
            credit = None
            credit_id = known_credit
            if getattr(adapter, "supports_discount_credit", False) is True:
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
            if credit is not None and not credit.success:
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
            if result.success:
                voided = bool(result.erp_document_id)
                action = "payment.erp_voided" if voided else "payment.erp_void_not_needed"
            else:
                action = "payment.erp_void_failed"
            await dispatch_audit(
                db,
                correlation_id=correlation,
                organization_id=invoice.organization_id,
                actor_id=None,
                action=action,
                entity_type="payment",
                entity_id=payment_id,
                details={
                    "erp_type": adapter.erp_type,
                    "bill_id": bill_id,
                    "erp_document_id": result.erp_document_id,
                    **({} if result.success else {"error": result.message}),
                    **({"refusal": refusal} if refusal else {}),
                    **(
                        {"document": "discount_credit"}
                        if credit is not None and result is credit
                        else {}
                    ),
                },
            )
            if result.success:
                await _clear_pending(db, invoice_id, payment_id)
                await _set_map_entry(db, invoice_id, PENDING_VOID_CREDITS_KEY, payment_id, None)
                await _set_pending_credit(db, invoice_id, payment_id, None)
            else:
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
                        failure=refusal["code"] if refusal else "erp_void_failed",
                        refusal=refusal,
                        retryable=result.retryable,
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
                )
            await db.commit()
            return WROTE if result.success else FAILED
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
