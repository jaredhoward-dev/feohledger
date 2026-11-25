"""Poll a generic ERP for the status of bills it was sent (decisions §528).

The generic per-tenant webhook (``POST /api/erp/webhook/{erp_type}``) is the
only inbound status path for an ERP without a sweep of its own, and it drops a
delivery it cannot verify: when the tenant's ERP credentials cannot be opened,
or the source's unseal budget is spent (decisions §408, §525). Such a delivery
is answered with the same silent 204 as a forged one, so the ERP never
redelivers it. This sweep is the backstop: it asks the tenant's adapter
(``ErpAdapter.get_invoice_status``) and applies what it reads through
``services/erp_status_report.apply_erp_status_report``, the webhook's own
transition, so a status reaches the invoice the same way whichever channel
carried it.

**Which tenants.** Every tenant whose ERP block selects an adapter that is not
Merge.dev (its deliveries go to its own route, and its read-back is separate)
and does not report status itself (``supports_status_sync``: QuickBooks Online
and Xero, which have the ``erp-status-sync`` sweep). The mock adapter is
polled, so the sweep runs on a laptop (guard rail 7); it always reads ``open``.

**Which invoices.** One with an ERP document id at a status a webhook could
still move: :data:`POLLED_STATUSES`. ``done`` / ``paid`` / ``failed`` are not
polled: nothing more can be paid here, and polling every closed invoice for
ever would be unbounded. A bill is asked about only under the connection that
issued it: the push's recorded realm must be the tenant's current one (the
webhook's by-document rule, §417 / §423), and when the ERP went live is known
(§428) the push must have been confirmed after it, so an id another ERP issued
is never read against this one.

**What counts as a change.** ``open`` → ``posted``, ``paid`` → ``paid`` and
``cancelled`` → ``cancelled``, the generic webhook's own words. ``draft``,
``partially_paid``, ``unknown`` and ``settled_no_payment`` have no webhook
equivalent and are not applied (a zero balance with no payment is never a
payment, §303). The last status read is kept in the latest workflow
instance's ``state_data["erp_status_poll"]`` with the moment, and a report is
applied only when the status differs from the one recorded: so a resolved
"voided after posting" exception is not re-opened every tick, and an unchanged
``open`` bill costs one call and no write but the moment.

**Fair, and stops at the budget.** Each tick takes, per tenant, at most
``FEOH_ERP_STATUS_POLL_SWEEP_BATCH_SIZE`` invoices, least recently polled
first. Before each read the connection's call budget
(``ErpAdapter.call_budget().usage()``) is asked whether the org's calls are
paused; if so, or if the read itself is refused by the budget
(``ErpCallBudgetExhausted``), the tenant's pass ends there.

**Idempotent across replicas, by the invoice row lock.** The ERP is read
outside any transaction. The result is applied under the invoice's row lock,
after asking again whether it is still a candidate with the same document id
and whether the status read differs from the one recorded; the record is
written in the same transaction. A second replica that read the same status
then finds it recorded (or the invoice moved on) and does nothing.

A tenant whose ERP credentials cannot be opened is logged and skipped; it
never stops the others. Never moves money by itself: a ``paid`` report is
recorded through the same ERP-paid path the webhook uses. Off by default
(``FEOH_ERP_STATUS_POLL_SWEEP_ENABLED``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, or_, select, true
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import AuditLog, WorkflowInstance
from app.services.erp_adapters.base import ErpAdapter, ErpCallBudgetExhausted, ErpInvoiceStatus
from app.services.erp_auto_send_sweep import _connected_at
from app.services.erp_status_report import ERP_STATUS_MAP, NOOP, apply_erp_status_report
from app.services.sweep_health import SWEEP_ERP_STATUS_POLL, run_sweep_loop

logger = logging.getLogger(__name__)

#: Statuses at which a generic webhook could still move the invoice.
POLLED_STATUSES = (
    InvoiceStatus.sent_to_erp,
    InvoiceStatus.posted_in_erp,
    InvoiceStatus.payment_scheduled,
)

#: What the adapter read → the generic webhook's status word. A status with no
#: entry has no webhook equivalent and is not applied.
POLLED_STATUS_WORDS: dict[ErpInvoiceStatus, str] = {
    ErpInvoiceStatus.open: "posted",
    ErpInvoiceStatus.paid: "paid",
    ErpInvoiceStatus.cancelled: "cancelled",
}

#: ``erp_type`` recorded on what the sweep applies, and the ``source`` marker
#: on a transition's audit row.
POLL_SOURCE = "poll"
#: The ``state_data`` key holding the last status read and when.
STATE_KEY = "erp_status_poll"

ERP_CONFIRMED_ACTION = "invoice.erp_confirmed"


@dataclass
class StatusPollSweepResult:
    """One tick's counts. Integers only, so nothing here can carry a vendor field."""

    tenants_scanned: int = 0
    invoices_polled: int = 0
    #: A changed status was applied (transition, payment, exception, withdrawal).
    invoices_applied: int = 0
    #: Read, but nothing to apply (unchanged, no webhook equivalent, or moved on).
    invoices_unchanged: int = 0
    #: Tenants whose pass ended because the ERP call budget was used.
    tenants_deferred: int = 0
    #: Tenants skipped or whose pass raised (credentials unopenable, a query failed).
    failures: int = 0
    #: Reads or applications that raised for one invoice.
    invoice_failures: int = 0


@dataclass(frozen=True)
class _Candidate:
    invoice_id: uuid.UUID
    erp_document_id: str


def pollable_adapter(adapter: ErpAdapter | None, adapter_key: str) -> bool:
    """Is this tenant's adapter one the sweep polls?"""
    from app.services.erp_adapters.dispatcher import MERGE_DEV_ADAPTER_KEY

    if adapter is None or adapter_key == MERGE_DEV_ADAPTER_KEY:
        return False
    return not adapter.supports_status_sync


def _latest_instance():
    """The invoice's latest workflow instance, as a LATERAL subquery."""
    return (
        select(WorkflowInstance.id, WorkflowInstance.state_data)
        .where(WorkflowInstance.invoice_id == Invoice.id)
        .order_by(WorkflowInstance.created_at.desc(), WorkflowInstance.id.desc())
        .limit(1)
        .lateral()
    )


def _candidates_query(
    erp_config: dict,
    *,
    not_before: datetime | None,
    invoice_id: uuid.UUID | None = None,
):
    """``(invoice id, document id)`` of every invoice the sweep may poll, least
    recently polled first."""
    from app.services.erp_link_realm import connection_realm, legacy_links_trusted

    instance = _latest_instance()
    recorded_realm = instance.c.state_data["erp_realm"].astext
    realm = connection_realm(erp_config)
    if realm is None:
        realm_clause = recorded_realm.is_(None)
    elif legacy_links_trusted(erp_config, realm):
        realm_clause = or_(recorded_realm == realm, recorded_realm.is_(None))
    else:
        realm_clause = recorded_realm == realm
    query = (
        select(Invoice.id, Invoice.erp_document_id)
        .select_from(Invoice)
        .join(instance, true())
        .where(
            Invoice.status.in_(POLLED_STATUSES),
            Invoice.erp_document_id.is_not(None),
            Invoice.erp_document_id != "",
            realm_clause,
        )
    )
    if not_before is not None:
        query = query.where(
            exists().where(
                AuditLog.correlation_id == Invoice.correlation_id,
                AuditLog.action == ERP_CONFIRMED_ACTION,
                AuditLog.created_at >= not_before,
            )
        )
    if invoice_id is not None:
        return query.where(Invoice.id == invoice_id)
    polled_at = instance.c.state_data[STATE_KEY]["at"].astext
    return query.order_by(polled_at.asc().nulls_first(), Invoice.id).limit(
        settings.erp_status_poll_sweep_batch_size
    )


async def _budget_paused(adapter: ErpAdapter) -> bool:
    """Are this org's calls on its ERP paused (share or quota used, or a
    provider cool-down)? False for an ERP with no shared budget and when the
    budget cannot be read: the read itself is refused before any call then."""
    try:
        budget = adapter.call_budget()
        usage = await budget.usage() if budget is not None else None
    except Exception as exc:  # noqa: BLE001 — a brake, not a gate
        logger.info("[erp-status-poll] budget unread: %s", exc.__class__.__name__)
        return False
    return usage is not None and usage.paused is not None


#: :func:`_apply_one` outcomes.
_APPLIED, _UNCHANGED = "applied", "unchanged"


async def _apply_one(
    engine: AsyncEngine,
    org: Organization,
    erp_config: dict,
    erp_type: str,
    candidate: _Candidate,
    read: ErpInvoiceStatus,
    *,
    not_before: datetime | None,
) -> str:
    from app.services.workflow_engine import get_invoice_for_update

    word = POLLED_STATUS_WORDS.get(read)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        invoice = await get_invoice_for_update(db, candidate.invoice_id)
        still = (
            await db.execute(
                _candidates_query(
                    erp_config, not_before=not_before, invoice_id=candidate.invoice_id
                )
            )
        ).first()
        if still is None or still.erp_document_id != candidate.erp_document_id:
            await db.rollback()
            return _UNCHANGED
        instance = (
            await db.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.invoice_id == invoice.id)
                .order_by(WorkflowInstance.created_at.desc(), WorkflowInstance.id.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one()
        state = dict(instance.state_data or {})
        previous = state.get(STATE_KEY) if isinstance(state.get(STATE_KEY), dict) else {}
        outcome = NOOP
        if word is not None and previous.get("status") != read.value:
            outcome = await apply_erp_status_report(
                db,
                invoice,
                org_id=org.id,
                org=org,
                erp_type=erp_type,
                erp_status=word,
                target_status=ERP_STATUS_MAP[word],
                erp_document_id=candidate.erp_document_id,
                source=POLL_SOURCE,
            )
        # The transition may have written the instance; read it again so the
        # record never overwrites what the transition stored.
        await db.refresh(instance)
        instance.state_data = {
            **(instance.state_data or {}),
            STATE_KEY: {"status": read.value, "at": datetime.now(UTC).isoformat()},
        }
        flag_modified(instance, "state_data")
        await db.commit()
    return _UNCHANGED if outcome == NOOP else _APPLIED


async def _sweep_tenant(org: Organization, *, result: StatusPollSweepResult) -> None:
    from app.services import provider_credentials
    from app.services.credential_crypto import CredentialCryptoError
    from app.services.erp_adapters import get_erp_adapter
    from app.services.erp_adapters.dispatcher import resolve_adapter_key

    public = (org.settings or {}).get("erp")
    if not isinstance(public, dict) or not public:
        return
    adapter_key = resolve_adapter_key(public)
    try:
        async with control_session_factory() as ctrl:
            erp_config = await provider_credentials.provider_config(org, "erp", db=ctrl) or {}
    except CredentialCryptoError:
        # The same failure that drops the webhook's deliveries: logged for the
        # operator, the tenant skipped, the others carry on.
        result.failures += 1
        logger.error(
            "[erp-status-poll] the ERP credentials of org %s could not be opened; skipped",
            org.id,
        )
        return
    try:
        adapter = get_erp_adapter(erp_config)
    except Exception as exc:  # noqa: BLE001 — an unknown adapter is not polled
        logger.info("[erp-status-poll] org %s: no adapter (%s)", org.id, exc.__class__.__name__)
        return
    if not pollable_adapter(adapter, adapter_key):
        return
    result.tenants_scanned += 1
    not_before = _connected_at(org.settings)
    engine = get_tenant_engine(org.db_name)
    async with AsyncSession(engine) as db:
        rows = (await db.execute(_candidates_query(erp_config, not_before=not_before))).all()
    for row in rows:
        candidate = _Candidate(row.id, row.erp_document_id)
        if await _budget_paused(adapter):
            result.tenants_deferred += 1
            return
        try:
            read = await adapter.get_invoice_status(candidate.erp_document_id)
        except ErpCallBudgetExhausted:
            result.tenants_deferred += 1
            return
        except Exception as exc:  # noqa: BLE001 — one bill must not halt the pass
            result.invoice_failures += 1
            logger.warning(
                "[erp-status-poll] status read for invoice %s failed: %s",
                candidate.invoice_id,
                exc.__class__.__name__,
            )
            continue
        result.invoices_polled += 1
        try:
            outcome = await _apply_one(
                engine,
                org,
                erp_config,
                adapter_key,
                candidate,
                ErpInvoiceStatus(read),
                not_before=not_before,
            )
        except Exception as exc:  # noqa: BLE001 — one invoice must not halt the pass
            result.invoice_failures += 1
            logger.warning(
                "[erp-status-poll] applying invoice %s failed: %s",
                candidate.invoice_id,
                exc.__class__.__name__,
            )
            continue
        if outcome == _APPLIED:
            result.invoices_applied += 1
        else:
            result.invoices_unchanged += 1


async def run_status_poll_sweep_once() -> StatusPollSweepResult:
    """One pass over every tenant. One tenant's failure never stops the others."""
    result = StatusPollSweepResult()
    if not settings.erp_status_poll_sweep_enabled:
        return result
    async with control_session_factory() as ctrl:
        orgs = list((await ctrl.execute(select(Organization))).scalars())
    for org in orgs:
        try:
            await _sweep_tenant(org, result=result)
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            result.failures += 1
            logger.warning("[erp-status-poll] org %s failed: %s", org.id, exc.__class__.__name__)
    if result.invoices_applied or result.failures or result.invoice_failures:
        logger.info(
            "[erp-status-poll] %d tenant(s); %d polled, %d applied, %d deferred tenant(s); "
            "%d failed invoice(s), %d failed tenant(s)",
            result.tenants_scanned,
            result.invoices_polled,
            result.invoices_applied,
            result.tenants_deferred,
            result.invoice_failures,
            result.failures,
        )
    return result


async def run_status_poll_sweep_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; the body is
    ``sweep_health.run_sweep_loop``, like every other sweep."""
    await run_sweep_loop(
        SWEEP_ERP_STATUS_POLL,
        lambda: run_status_poll_sweep_once(),
        interval_seconds=settings.erp_status_poll_sweep_interval_seconds,
        log=logger,
        log_prefix="[erp-status-poll]",
        start_detail=f" batch={settings.erp_status_poll_sweep_batch_size}",
    )
