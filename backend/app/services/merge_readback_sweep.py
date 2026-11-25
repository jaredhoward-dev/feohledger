"""Read back the Merge.dev bills we pushed that Merge has changed (decisions §540).

The Merge webhook (``POST /api/erp/webhook/merge_dev/{slug}``) drops a delivery
it cannot verify: when the tenant's credentials cannot be opened, or the
source's unseal budget is spent for a tenant whose data key is cold (decisions
§408, §525, §527). Such a delivery is answered with the same silent 204 as a
forged one and Merge never redelivers it, so a void in the ERP was never
flagged. This sweep is the backstop: it asks each Merge tenant's change feed
(``MergeDevAdapter.changed_bills``) for the bills changed since a stored
cursor, keeps those an invoice here was pushed as (``_bills_we_posted``, the
webhook's own pre-read filter), reads each back (``bill_report``) and applies
it with ``erp_status_sync.apply_bill_report`` — the reconciler the webhook and
the QuickBooks / Xero status sync use. One reconciler, three channels: a bill
means the same here whichever carried it.

**The cursor** is the tenant's ``Organization.settings["erp_merge_readback"]``:
``through`` (the moment the last listing started; the next one asks Merge for
changes after it) and ``pending`` (bills of that listing not yet applied). The
QuickBooks / Xero sync keeps its mark in ``settings.erp.oauth``, but a Merge
connection has no OAuth block, and a key inside ``settings.erp`` would ride
into the adapter config and the connection's realm. A first pass (no cursor)
lists the last ``FEOH_MERGE_READBACK_SWEEP_LOOKBACK_HOURS``.

**The cursor moves only past what was applied.** A listing's matched bills are
read back in order, at most ``FEOH_MERGE_READBACK_SWEEP_BATCH_SIZE`` a tick;
what is left stays ``pending`` and is worked off before the next listing. A read
that fails, or that the call budget refuses, ends the tenant's pass and keeps
that bill and everything after it pending. A listing that fails moves nothing.

**Replica-safe by compare-and-swap.** The cursor is written under the org row
lock (like every settings writer) only if it is still what this pass read; a
second replica that ran the same window finds it moved and writes nothing.
Applying a bill twice changes nothing (``apply_bill_report`` re-checks under
the invoice row lock and its flags dedupe while live), so two replicas applying
the same listing apply it once.

A bill Merge answers 404 for is not read as gone: only Merge's
``Invoice.removed`` says that, and this feed carries no event (§399). It is
dropped from ``pending`` and counted.

A tenant whose ERP credentials cannot be opened is logged (no exception text)
and skipped; the others carry on. Never moves money: ``apply_bill_report``
only flags. Off by default (``FEOH_MERGE_READBACK_SWEEP_ENABLED``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.organization import Organization
from app.services.erp_adapters.base import ErpAdapter, ErpCallBudgetExhausted
from app.services.sweep_health import SWEEP_MERGE_READBACK, run_sweep_loop

logger = logging.getLogger(__name__)

#: ``Organization.settings`` key holding the per-tenant cursor.
CURSOR_KEY = "erp_merge_readback"
MERGE_PROVIDER = "merge_dev"


@dataclass
class MergeReadbackResult:
    """One tick's counts. Integers only, so nothing here can carry a vendor field."""

    tenants_scanned: int = 0
    bills_listed: int = 0
    bills_read: int = 0
    #: ``apply_bill_report`` changed something (a flag, a withdrawal).
    bills_applied: int = 0
    #: Merge answered 404 for a listed bill (not read as gone).
    bills_not_found: int = 0
    #: Tenants whose pass ended because the ERP call budget was used.
    tenants_deferred: int = 0
    #: Tenants skipped or whose pass raised (credentials unopenable, a listing
    #: or a read failed).
    failures: int = 0


def _cursor(org: Organization) -> dict:
    raw = (org.settings or {}).get(CURSOR_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


async def _budget_paused(adapter: ErpAdapter) -> bool:
    """Are this org's calls on its ERP paused? False for an ERP with no shared
    budget (Merge's own today) and when the budget cannot be read: a read the
    budget refuses raises ``ErpCallBudgetExhausted`` before any call then."""
    try:
        budget = adapter.call_budget()
        usage = await budget.usage() if budget is not None else None
    except Exception as exc:  # noqa: BLE001 — a brake, not a gate
        logger.info("[merge-readback] budget unread: %s", exc.__class__.__name__)
        return False
    return usage is not None and usage.paused is not None


async def _save_cursor(org_id, expected: dict, new: dict) -> bool:
    """Write ``new`` only if the stored cursor is still ``expected``. Under the
    org row lock. Returns whether it wrote."""
    async with control_session_factory() as ctrl:
        org = (
            await ctrl.execute(
                select(Organization).where(Organization.id == org_id).with_for_update()
            )
        ).scalar_one_or_none()
        if org is None or _cursor(org) != expected:
            await ctrl.rollback()
            return False
        org.settings = {**(org.settings or {}), CURSOR_KEY: new}
        flag_modified(org, "settings")
        await ctrl.commit()
    return True


async def _sweep_tenant(org: Organization, *, now: datetime, result: MergeReadbackResult) -> None:
    from app.services import provider_credentials
    from app.services.credential_crypto import CredentialCryptoError
    from app.services.erp_adapters import get_erp_adapter
    from app.services.erp_adapters.dispatcher import MERGE_DEV_ADAPTER_KEY, resolve_adapter_key
    from app.services.erp_adapters.merge_dev import MergeBillNotFound
    from app.services.erp_link_realm import connection_realm
    from app.services.erp_status_sync import _bills_we_posted, apply_bill_report

    public = (org.settings or {}).get("erp")
    if not isinstance(public, dict) or not public:
        return
    if resolve_adapter_key(public) != MERGE_DEV_ADAPTER_KEY:
        return
    try:
        async with control_session_factory() as ctrl:
            erp_config = await provider_credentials.provider_config(org, "erp", db=ctrl) or {}
    except CredentialCryptoError:
        # The same failure that drops the webhook's deliveries: logged for the
        # operator without the exception's text, the tenant skipped.
        result.failures += 1
        logger.error(
            "[merge-readback] the ERP credentials of org %s could not be opened; skipped",
            org.id,
        )
        return
    adapter = get_erp_adapter(erp_config)
    result.tenants_scanned += 1
    realm = connection_realm(erp_config)
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)

    expected = _cursor(org)
    pending = [b for b in expected.get("pending") or [] if isinstance(b, str) and b]
    through = expected.get("through")
    if not pending:
        if await _budget_paused(adapter):
            result.tenants_deferred += 1
            return
        lookback = timedelta(hours=settings.merge_readback_sweep_lookback_hours)
        since = _parse(through) or (now - lookback)
        try:
            changed = await adapter.changed_bills(since)
        except ErpCallBudgetExhausted:
            result.tenants_deferred += 1
            return
        result.bills_listed += len(changed)
        pending = await _bills_we_posted(factory, changed, realm, by_reference=True)
        through = now.isoformat()

    done = 0
    for bill_id in pending[: settings.merge_readback_sweep_batch_size]:
        if await _budget_paused(adapter):
            result.tenants_deferred += 1
            break
        try:
            report = await adapter.bill_report(bill_id)
        except MergeBillNotFound:
            result.bills_not_found += 1
            done += 1
            continue
        except ErpCallBudgetExhausted:
            result.tenants_deferred += 1
            break
        except Exception as exc:  # noqa: BLE001 — kept pending for the next tick
            result.failures += 1
            logger.warning(
                "[merge-readback] org %s: bill read failed: %s", org.id, exc.__class__.__name__
            )
            break
        result.bills_read += 1
        async with factory() as db:
            outcome = await apply_bill_report(
                db, org=org, report=report, realm=realm, erp_type=MERGE_PROVIDER, event_id=None
            )
        if outcome.action not in ("unmatched", "unchanged"):
            result.bills_applied += 1
        done += 1
    new = {"through": through, "pending": pending[done:]}
    if new != expected:
        await _save_cursor(org.id, expected, new)


async def run_merge_readback_once(*, now: datetime | None = None) -> MergeReadbackResult:
    """One pass over every Merge.dev tenant. One tenant's failure never stops
    the others."""
    result = MergeReadbackResult()
    if not settings.merge_readback_sweep_enabled:
        return result
    moment = now or datetime.now(UTC)
    async with control_session_factory() as ctrl:
        orgs = list((await ctrl.execute(select(Organization))).scalars())
    for org in orgs:
        try:
            await _sweep_tenant(org, now=moment, result=result)
        except Exception as exc:  # noqa: BLE001 — one tenant must not halt the sweep
            result.failures += 1
            logger.warning("[merge-readback] org %s failed: %s", org.id, exc.__class__.__name__)
    if result.bills_applied or result.failures:
        logger.info(
            "[merge-readback] %d tenant(s); %d listed, %d read, %d applied, %d not found, "
            "%d deferred tenant(s), %d failure(s)",
            result.tenants_scanned,
            result.bills_listed,
            result.bills_read,
            result.bills_applied,
            result.bills_not_found,
            result.tenants_deferred,
            result.failures,
        )
    return result


async def run_merge_readback_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; the body is
    ``sweep_health.run_sweep_loop``, like every other sweep."""
    await run_sweep_loop(
        SWEEP_MERGE_READBACK,
        lambda: run_merge_readback_once(),
        interval_seconds=settings.merge_readback_sweep_interval_seconds,
        log=logger,
        log_prefix="[merge-readback]",
        start_detail=f" batch={settings.merge_readback_sweep_batch_size}",
    )
