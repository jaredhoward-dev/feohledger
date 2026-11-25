"""Re-send a push the ERP call budget refused, once the allowance resets (decisions §521).

A push to an ERP whose API quota several tenants share (Blackbaud's SKY API
today) is refused **before any call** when the org's share of the day is used,
or the subscription's whole quota is (``services/erp_call_budget``, §322). The
invoice lands at ``failed`` with an ``invoice.erp_failed`` row coded
``erp_call_budget_exhausted`` whose params carry ``reason`` and ``retry_after``
(ISO-8601 UTC; the next 00:00 UTC for a used share or quota). Nothing was sent,
so nothing is half done in the ERP; until this sweep someone had to retry each
invoice by hand after the reset.

**What counts as due.** An invoice is a candidate when all of these hold:

* it is ``failed`` now, and its latest ``invoice.erp_failed`` row carries the
  ``erp_call_budget_exhausted`` refusal with a readable ``retry_after`` that has
  passed (an unreadable one is never guessed at: a person retries it);
* that refusal is younger than ``FEOH_ERP_BUDGET_RETRY_SWEEP_LOOKBACK_HOURS``
  and no older than the tenant's current ERP going live (the later of an OAuth
  connection's ``connected_at`` and ``settings.erp.configured_at``, the fence
  the auto-send recovery uses, §428). The lookback is what makes turning the
  sweep on safe: an old refusal may have been keyed into the ERP by hand since,
  and a refusal from before the ERP changed was meant for a different one;
* **nothing has happened to the invoice since**: no audit row on its trail after
  that refusal except a read of the trail itself (``audit.viewed`` /
  ``audit.exported``). An edit, a comment, an exception raised or a retry by
  hand all mean a person has the invoice, and the sweep leaves it to them. A
  re-send the budget refuses again writes a new refusal row, which is then the
  latest and is due at the next reset.

**One path.** Each candidate goes through ``erp_send.start_erp_send`` with
``retry=True``, the ``POST /{id}/retry-erp`` path: the status gate (an approved
invoice's failed push), the plan gate (§258) and the export gate (§286 / §309 /
§333) all apply. The ``invoice.erp_retried`` row says
``trigger: erp_call_budget_reset`` and the refusal's ``retry_after``; the actor
is NULL (nobody asked). A gate that refuses is recorded once as
``invoice.erp_budget_resend_refused`` with the coded refusal, which also takes
the invoice out of the candidate set: a person decides from there.

**Earliest ``retry_after`` first, and it stops at the budget.** Within a tenant
the due invoices are taken in ``retry_after`` order, at most
``FEOH_ERP_BUDGET_RETRY_SWEEP_BATCH_SIZE`` a tick. Before each, the
connection's own call budget (``ErpAdapter.call_budget``, read-only
``usage()``) is asked whether the org's calls are paused (share used, quota
used, or a provider cool-down); if so the tenant's pass ends there. In local
dispatch mode each push runs to completion before the next, so a push the
budget refused again ends the pass too. In Lambda mode the push is a queued
message and its outcome is not seen here; the pre-check is the brake, and a
push that still finds the share used is refused before any call, exactly as
the first time, and is picked up at the next reset. A cap, not a page, is sound
(``backend/docs/background-sweeps.md`` § Locking) because every candidate the
pass reaches leaves the set (re-sent, refused with a row, or re-refused with a
later ``retry_after``) and the ones it does not reach are, by the order, the
latest due.

**Idempotent across replicas, by the invoice row lock.** Each candidate is
asked again under its row lock, and only if its latest refusal is still the very
row the selection read is it sent; the send moves it to ``sending_to_erp``
under that lock. A second replica then finds it moved (or re-refused with a
later ``retry_after``) and does nothing. A gate refusal is recorded only after
the same question is asked again under a fresh lock, so two replicas record
one. (In ``FEOH_AUDIT_MODE=lambda`` that row lands later through SQS, so a
second replica in the same instant can record a second; the push is guarded by
the row lock either way, as for the auto-send refusal, §411.) No cross-request
lock is held for the pass.

Never moves money: it books a bill in the customer's ERP, exactly what the
retry by hand would have done. Off by default
(``FEOH_ERP_BUDGET_RETRY_SWEEP_ENABLED``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import exists, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services import erp_send
from app.services.erp_adapters.base import ERP_CALL_BUDGET_EXHAUSTED
from app.services.erp_auto_send_sweep import _connected_at
from app.services.sweep_health import SWEEP_ERP_BUDGET_RETRY, run_sweep_loop

logger = logging.getLogger(__name__)

#: The ``trigger`` on the ``invoice.erp_retried`` row and on a refusal row.
BUDGET_RESEND_TRIGGER = "erp_call_budget_reset"
#: A gate refused the sweep's re-send; the invoice stays ``failed``.
BUDGET_RESEND_REFUSED_ACTION = "invoice.erp_budget_resend_refused"

ERP_FAILED_ACTION = "invoice.erp_failed"
#: Action suffixes of rows that read a record without changing it: ``log_access``
#: writes ``<entity_type>.viewed`` and the auditor export ``audit.exported``
#: (the same reads ``access_review`` discounts). Matched by suffix, so a read of
#: the invoice or its trail never counts as the invoice having changed.
READ_ONLY_ACTION_SUFFIXES = (".viewed", ".exported")


@dataclass
class BudgetRetrySweepResult:
    """One tick's counts. Integers only, so nothing here can carry a vendor field."""

    tenants_scanned: int = 0
    invoices_resent: int = 0
    #: A gate refused the re-send; recorded, left ``failed`` for a person.
    invoices_refused: int = 0
    #: Candidates with nothing to do by the time they were reached.
    invoices_skipped: int = 0
    #: Tenants whose pass ended because the budget was used again.
    tenants_deferred: int = 0
    #: Tenants whose pass raised (could not connect, a query failed).
    failures: int = 0
    #: Re-sends that raised and left nothing on the trail (``partial`` tick).
    invoice_failures: int = 0


@dataclass(frozen=True)
class _Candidate:
    invoice_id: uuid.UUID
    refusal_id: uuid.UUID
    retry_after: datetime


def _latest_refusal():
    """The invoice's latest ``invoice.erp_failed`` row, as a LATERAL subquery."""
    return (
        select(AuditLog.id, AuditLog.created_at, AuditLog.details)
        .where(
            AuditLog.correlation_id == Invoice.correlation_id,
            AuditLog.entity_id == Invoice.id,
            AuditLog.action == ERP_FAILED_ACTION,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
        .lateral()
    )


def _candidates_query(*, floor: datetime, invoice_id: uuid.UUID | None = None):
    """``(invoice id, refusal row id, refusal details)`` of every ``failed``
    invoice whose latest push failure is a call-budget refusal made at or after
    ``floor``, with nothing on its trail since but reads of the trail. The
    ``retry_after`` test is done in Python (:func:`_due`): a malformed stored
    value must skip the row, not fail the query for the whole tenant."""
    refusal = _latest_refusal()
    touched_since = exists().where(
        AuditLog.correlation_id == Invoice.correlation_id,
        AuditLog.entity_id == Invoice.id,
        AuditLog.created_at > refusal.c.created_at,
        not_(or_(*(AuditLog.action.endswith(s) for s in READ_ONLY_ACTION_SUFFIXES))),
    )
    query = (
        select(Invoice.id, refusal.c.id.label("refusal_id"), refusal.c.details)
        .select_from(Invoice)
        .join(refusal, true())
        .where(
            Invoice.status == InvoiceStatus.failed,
            refusal.c.details["refusal"]["code"].astext == ERP_CALL_BUDGET_EXHAUSTED,
            refusal.c.created_at >= floor,
            not_(touched_since),
        )
    )
    if invoice_id is not None:
        query = query.where(Invoice.id == invoice_id)
    return query


def _retry_after(details: object) -> datetime | None:
    """The refusal's ``params.retry_after``, or None when absent or unreadable."""
    if not isinstance(details, dict):
        return None
    refusal = details.get("refusal")
    params = refusal.get("params") if isinstance(refusal, dict) else None
    raw = params.get("retry_after") if isinstance(params, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _floor(now: datetime, not_before: datetime | None) -> datetime:
    floor = now - timedelta(hours=settings.erp_budget_retry_sweep_lookback_hours)
    return max(floor, not_before) if not_before is not None else floor


async def _due(engine: AsyncEngine, *, now: datetime, floor: datetime) -> list[_Candidate]:
    """The tenant's due candidates, earliest ``retry_after`` first, capped."""
    async with AsyncSession(engine) as db:
        rows = (await db.execute(_candidates_query(floor=floor))).all()
    due = []
    for row in rows:
        retry_after = _retry_after(row.details)
        if retry_after is not None and retry_after <= now:
            due.append(_Candidate(row.id, row.refusal_id, retry_after))
    due.sort(key=lambda c: (c.retry_after, str(c.invoice_id)))
    return due[: settings.erp_budget_retry_sweep_batch_size]


async def _still_due(
    db: AsyncSession, candidate: _Candidate, *, floor: datetime, now: datetime
) -> bool:
    """Asked under the caller's row lock: is the latest refusal still the row the
    selection read, and still due?"""
    row = (
        await db.execute(_candidates_query(floor=floor, invoice_id=candidate.invoice_id))
    ).first()
    if row is None or row.refusal_id != candidate.refusal_id:
        return False
    retry_after = _retry_after(row.details)
    return retry_after is not None and retry_after <= now


async def _budget_paused(org: Organization) -> bool:
    """Are this org's calls on its ERP paused right now (share or quota used, or
    a provider cool-down)? False for an ERP with no shared budget, and when the
    budget cannot be read: the push itself checks again before any call."""
    from app.services import provider_credentials
    from app.services.erp_adapters import get_erp_adapter

    try:
        async with control_session_factory() as ctrl:
            resolved = await provider_credentials.provider_config(org, "erp", db=ctrl)
        budget = get_erp_adapter(resolved or {}).call_budget()
        usage = await budget.usage() if budget is not None else None
    except Exception as exc:  # noqa: BLE001 — a brake, not a gate; the push gates itself
        logger.info("[erp-budget-retry] budget unread: %s", exc.__class__.__name__)
        return False
    return usage is not None and usage.paused is not None


async def _refused_again(engine: AsyncEngine, candidate: _Candidate) -> bool:
    """Did the re-send just made end in a new call-budget refusal?"""
    async with AsyncSession(engine) as db:
        latest = (
            await db.execute(
                select(AuditLog.id, AuditLog.details)
                .join(Invoice, Invoice.id == AuditLog.entity_id)
                .where(
                    Invoice.id == candidate.invoice_id,
                    AuditLog.correlation_id == Invoice.correlation_id,
                    AuditLog.action == ERP_FAILED_ACTION,
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(1)
            )
        ).first()
    if latest is None or latest.id == candidate.refusal_id:
        return False
    refusal = (latest.details or {}).get("refusal") if isinstance(latest.details, dict) else None
    return isinstance(refusal, dict) and refusal.get("code") == ERP_CALL_BUDGET_EXHAUSTED


#: :func:`_resend_one` outcomes.
_RESENT, _REFUSED, _SKIPPED = "resent", "refused", "skipped"


async def _resend_one(
    engine: AsyncEngine, org: Organization, candidate: _Candidate, *, floor: datetime
) -> str:
    from app.services.workflow_engine import get_invoice_for_update

    marker = {"retry_after": candidate.retry_after.isoformat()}
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await get_invoice_for_update(db, candidate.invoice_id)
        if not await _still_due(db, candidate, floor=floor, now=datetime.now(UTC)):
            await db.rollback()
            return _SKIPPED
        try:
            # The lock is held into `start_erp_send`, which re-takes it in this
            # transaction: nobody can move the invoice between the question
            # above and the transition to `sending_to_erp`.
            sent = await start_erp_send_for_budget(db, org, candidate.invoice_id, marker)
        except HTTPException as exc:
            await db.rollback()
            return await _record_refusal(db, org, candidate, exc, floor=floor, marker=marker)
    return _RESENT if sent else _SKIPPED


async def start_erp_send_for_budget(
    db: AsyncSession, org: Organization, invoice_id: uuid.UUID, marker: dict
) -> bool:
    """The retry, through the one push path. Awaited to completion in local
    mode (one push at a time, so the next is decided on this one's outcome)."""
    sent = await erp_send.start_erp_send(
        db,
        org_id=org.id,
        org_settings=org.settings,
        invoice_id=invoice_id,
        actor_id=None,
        retry=True,
        trigger=BUDGET_RESEND_TRIGGER,
        trigger_details=marker,
        only_from=InvoiceStatus.failed,
        inline_dispatch=True,
    )
    return sent is not None


async def _record_refusal(
    db: AsyncSession,
    org: Organization,
    candidate: _Candidate,
    exc: HTTPException,
    *,
    floor: datetime,
    marker: dict,
) -> str:
    """Record a gate's refusal once: asked again under a fresh row lock, so a
    second replica refused at the same moment finds the first's row (it is on
    the trail after the refusal, so the invoice is no longer due) and skips."""
    from app.services.workflow_engine import get_invoice_for_update

    invoice = await get_invoice_for_update(db, candidate.invoice_id)
    if not await _still_due(db, candidate, floor=floor, now=datetime.now(UTC)):
        await db.rollback()
        return _SKIPPED
    await erp_send._record_refusal(
        db,
        invoice_id=candidate.invoice_id,
        correlation_id=invoice.correlation_id,
        organization_id=invoice.organization_id,
        actor_id=None,
        exc=exc,
        extra=marker,
        action=BUDGET_RESEND_REFUSED_ACTION,
        trigger=BUDGET_RESEND_TRIGGER,
    )
    await db.commit()
    return _REFUSED


async def _sweep_tenant(
    org: Organization, *, now: datetime, result: BudgetRetrySweepResult
) -> None:
    engine = get_tenant_engine(org.db_name)
    floor = _floor(now, _connected_at(org.settings))
    due = await _due(engine, now=now, floor=floor)
    for candidate in due:
        if await _budget_paused(org):
            result.tenants_deferred += 1
            return
        try:
            outcome = await _resend_one(engine, org, candidate, floor=floor)
        except Exception as exc:  # noqa: BLE001 — one invoice must not halt the pass
            result.invoice_failures += 1
            logger.warning(
                "[erp-budget-retry] re-send of invoice %s failed: %s",
                candidate.invoice_id,
                exc.__class__.__name__,
            )
            continue
        if outcome == _RESENT:
            result.invoices_resent += 1
            if await _refused_again(engine, candidate):
                # The share is used again: the rest wait for the next reset.
                result.tenants_deferred += 1
                return
        elif outcome == _REFUSED:
            result.invoices_refused += 1
        else:
            result.invoices_skipped += 1


async def run_budget_retry_sweep_once(*, now: datetime | None = None) -> BudgetRetrySweepResult:
    """One pass over every tenant with a live ERP. One tenant's failure never
    stops the others."""
    from app.services.erp_adapters.dispatcher import erp_config_is_live

    result = BudgetRetrySweepResult()
    if not settings.erp_budget_retry_sweep_enabled:
        return result
    moment = now or datetime.now(UTC)
    async with control_session_factory() as ctrl:
        orgs = list((await ctrl.execute(select(Organization))).scalars())
    for org in orgs:
        if not erp_config_is_live((org.settings or {}).get("erp")):
            continue
        result.tenants_scanned += 1
        try:
            await _sweep_tenant(org, now=moment, result=result)
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            result.failures += 1
            logger.warning("[erp-budget-retry] org %s failed: %s", org.id, exc.__class__.__name__)
    if (
        result.invoices_resent
        or result.invoices_refused
        or result.failures
        or result.invoice_failures
    ):
        logger.info(
            "[erp-budget-retry] %d tenant(s); %d re-sent, %d refused, %d deferred tenant(s); "
            "%d failed re-send(s), %d failed tenant(s)",
            result.tenants_scanned,
            result.invoices_resent,
            result.invoices_refused,
            result.tenants_deferred,
            result.invoice_failures,
            result.failures,
        )
    return result


async def run_budget_retry_sweep_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; the body is
    ``sweep_health.run_sweep_loop``, like every other sweep."""
    await run_sweep_loop(
        SWEEP_ERP_BUDGET_RETRY,
        lambda: run_budget_retry_sweep_once(),
        interval_seconds=settings.erp_budget_retry_sweep_interval_seconds,
        log=logger,
        log_prefix="[erp-budget-retry]",
        start_detail=(
            f" lookback={settings.erp_budget_retry_sweep_lookback_hours}h"
            f" batch={settings.erp_budget_retry_sweep_batch_size}"
        ),
    )
