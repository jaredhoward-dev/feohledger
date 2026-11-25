"""The call-budget re-send sweep (decisions §521).

A push the shared ERP call budget refused (`erp_call_budget_exhausted`, §322)
fails before any call, with the reason and `retry_after`. The sweep re-sends it
once the allowance has reset, through `erp_send.start_erp_send`.

Pinned here, on real Postgres:

* a due refusal is re-sent once, as a retry with no actor and the sweep's
  trigger, and not before its `retry_after`;
* an invoice touched since the refusal (an edit, a later failure for another
  reason) is left to a person, while a read of the trail is not a touch;
* the lookback and the ERP-went-live fence, and an unreadable `retry_after`;
* earliest `retry_after` first, and the pass stops when the budget is used
  again: a push refused again, or the budget paused before the next;
* two replicas at once send once, and a gate refusal is recorded once;
* off by default, and registered with the health report.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.config import Settings, settings
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import AuditLog, WorkflowInstance
from app.services import erp_budget_retry_sweep as sweep
from app.services.erp_adapters.base import ERP_CALL_BUDGET_EXHAUSTED

TENANT = "a"
LIVE_ERP = {"type": "netsuite", "integration_method": "direct", "account_id": "1234567"}

pytestmark = pytest.mark.plan("growth")


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "erp_budget_retry_sweep_enabled", True)
    monkeypatch.setattr(settings, "erp_budget_retry_sweep_lookback_hours", 48)
    monkeypatch.setattr(settings, "erp_budget_retry_sweep_batch_size", 50)


@pytest.fixture
def dispatched(monkeypatch):
    """Records each dispatch; the push itself is not run."""
    calls: list[uuid.UUID] = []

    async def _record(invoice_id, org_id, actor_id, **_kwargs):
        assert actor_id is None
        calls.append(invoice_id)

    monkeypatch.setattr("app.services.erp_send.dispatch_erp", _record)
    return calls


async def _write_erp(realdb, erp: dict) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        org.settings = {**(org.settings or {}), "erp": erp}
        flag_modified(org, "settings")
        await s.commit()


def _ago(**delta) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


def _budget_refusal(retry_after: object) -> dict:
    return {
        "code": ERP_CALL_BUDGET_EXHAUSTED,
        "params": {"reason": "tenant_budget", "retry_after": retry_after, "provider": "x"},
    }


async def _seed_failed(realdb) -> uuid.UUID:
    """An approved invoice whose push failed, with no trail yet."""
    from app.services.workflow_engine import get_or_create_workflow_definition

    info = realdb.info(TENANT)
    inv_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=info.org_id,
            invoice_number=f"BUDGET-{inv_id.hex[:8]}",
            vendor_name="Northwind Supplies",
            amount=Decimal("1234.56"),
            currency="USD",
            status=InvoiceStatus.failed,
            approved_by="Pat Manager",
            uploaded_by_id=None,
        )
        s.add(inv)
        await s.flush()
        defn = await get_or_create_workflow_definition(s, info.org_id)
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=defn.id,
                invoice_id=inv_id,
                current_step=3,
                state="failed",
                state_data={"erp_retries": 1},
                steps_config_snapshot={"steps": []},
            )
        )
        await s.commit()
    return inv_id


async def _trail(
    realdb,
    inv_id,
    action: str,
    at: datetime,
    details: dict | None = None,
    *,
    entity_type: str = "invoice",
    entity_id: uuid.UUID | None = None,
) -> None:
    async with realdb.sessionmaker(TENANT)() as s:
        inv = await s.get(Invoice, inv_id)
        s.add(
            AuditLog(
                correlation_id=inv.correlation_id,
                organization_id=inv.organization_id,
                actor_id=None,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id or inv_id,
                details=details or {},
                created_at=at,
            )
        )
        await s.commit()


async def _refuse(realdb, inv_id, *, at: datetime, retry_after: object) -> None:
    await _trail(
        realdb,
        inv_id,
        sweep.ERP_FAILED_ACTION,
        at,
        {"new_status": "failed", "error": "budget", "refusal": _budget_refusal(retry_after)},
    )


async def _seed_refused(
    realdb, *, refused_ago=timedelta(hours=3), retry_after_ago=timedelta(hours=1)
) -> uuid.UUID:
    inv_id = await _seed_failed(realdb)
    await _refuse(
        realdb,
        inv_id,
        at=datetime.now(UTC) - refused_ago,
        retry_after=(datetime.now(UTC) - retry_after_ago).isoformat(),
    )
    return inv_id


async def _status(realdb, inv_id) -> InvoiceStatus:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(Invoice, inv_id)).status


async def _audit(realdb, inv_id, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == inv_id, AuditLog.action == action)
        )
        return list(rows.scalars())


# ---------------------------------------------------------------------------
# re-sent after the reset, not before
# ---------------------------------------------------------------------------


async def test_a_due_refusal_is_re_sent_once_as_a_retry(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    retry_after = _ago(hours=1)
    inv_id = await _seed_failed(realdb)
    await _refuse(realdb, inv_id, at=_ago(hours=3), retry_after=retry_after.isoformat())

    result = await sweep.run_budget_retry_sweep_once()

    assert result.invoices_resent == 1
    assert result.failures == 0 and result.invoice_failures == 0
    assert dispatched == [inv_id]
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp
    [retried] = await _audit(realdb, inv_id, "invoice.erp_retried")
    assert retried.actor_id is None
    assert retried.details["trigger"] == sweep.BUDGET_RESEND_TRIGGER
    assert retried.details["retry_after"] == retry_after.isoformat()

    again = await sweep.run_budget_retry_sweep_once()

    assert again.invoices_resent == 0
    assert dispatched == [inv_id]


async def test_a_refusal_whose_allowance_has_not_reset_is_left(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_refused(
        realdb, refused_ago=timedelta(hours=1), retry_after_ago=-timedelta(hours=2)
    )

    result = await sweep.run_budget_retry_sweep_once()

    assert result.invoices_resent == 0
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.failed


@pytest.mark.parametrize("retry_after", ["not a time", None, 12])
async def test_an_unreadable_retry_after_is_never_guessed(realdb, dispatched, retry_after):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_failed(realdb)
    await _refuse(realdb, inv_id, at=_ago(hours=3), retry_after=retry_after)

    await sweep.run_budget_retry_sweep_once()

    assert dispatched == []


# ---------------------------------------------------------------------------
# unchanged since the refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "details"),
    [
        ("invoice.edited", {"fields": ["amount"]}),
        # A later failure for another reason is the latest push outcome now.
        (sweep.ERP_FAILED_ACTION, {"refusal": {"code": "vendor_not_linked", "params": {}}}),
    ],
    ids=["edited", "failed-again-otherwise"],
)
async def test_an_invoice_touched_since_the_refusal_is_left_to_a_person(
    realdb, dispatched, action, details
):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_refused(realdb)
    await _trail(realdb, inv_id, action, _ago(minutes=30), details)

    result = await sweep.run_budget_retry_sweep_once()

    assert result.invoices_resent == 0
    assert dispatched == []


async def test_an_exception_raised_on_the_invoice_since_the_refusal_is_a_change(realdb, dispatched):
    """An exception's rows are filed under the invoice's correlation id but
    against the exception row (``exception_lifecycle._audit``), so the check
    reads the invoice's whole trail, not only rows naming the invoice."""
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_refused(realdb)
    await _trail(
        realdb,
        inv_id,
        "exception.raised",
        _ago(minutes=30),
        {"exception_type": "po_mismatch"},
        entity_type="exception",
        entity_id=uuid.uuid4(),
    )

    result = await sweep.run_budget_retry_sweep_once()

    assert result.invoices_resent == 0
    assert dispatched == []


async def test_reading_the_trail_is_not_a_change(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_refused(realdb)
    await _trail(realdb, inv_id, "audit.viewed", _ago(minutes=30))

    await sweep.run_budget_retry_sweep_once()

    assert dispatched == [inv_id]


async def test_an_invoice_no_longer_failed_is_left(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_refused(realdb)
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(Invoice).where(Invoice.id == inv_id).values(status=InvoiceStatus.approved)
        )
        await s.commit()

    await sweep.run_budget_retry_sweep_once()

    assert dispatched == []


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------


async def test_a_refusal_older_than_the_lookback_is_left(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    await _seed_refused(
        realdb, refused_ago=timedelta(hours=49), retry_after_ago=timedelta(hours=30)
    )

    await sweep.run_budget_retry_sweep_once()

    assert dispatched == []


@pytest.mark.parametrize(
    ("configured_ago", "expect_sent"),
    [(timedelta(hours=2), False), (timedelta(hours=4), True)],
    ids=["refused-before-the-erp-was-saved", "refused-after-it"],
)
async def test_a_refusal_before_the_erp_went_live_is_left(
    realdb, dispatched, configured_ago, expect_sent
):
    configured_at = (datetime.now(UTC) - configured_ago).isoformat()
    await _write_erp(realdb, {**LIVE_ERP, "configured_at": configured_at})
    inv_id = await _seed_refused(realdb)  # refused 3 h ago

    await sweep.run_budget_retry_sweep_once()

    assert dispatched == ([inv_id] if expect_sent else [])


async def test_a_tenant_on_the_mock_erp_is_not_swept(realdb, dispatched):
    await _write_erp(realdb, {"type": "mock", "integration_method": "direct"})
    await _seed_refused(realdb)

    result = await sweep.run_budget_retry_sweep_once()

    assert result.tenants_scanned == 0 or result.invoices_resent == 0
    assert dispatched == []


# ---------------------------------------------------------------------------
# order, and the budget
# ---------------------------------------------------------------------------


async def test_earliest_retry_after_first_and_the_pass_stops_when_refused_again(
    realdb, monkeypatch
):
    """The push the sweep sends is refused by the budget again: the rest of
    the tenant's due invoices wait for the next reset."""
    await _write_erp(realdb, LIVE_ERP)
    later = await _seed_refused(realdb, retry_after_ago=timedelta(minutes=30))
    earlier = await _seed_refused(realdb, retry_after_ago=timedelta(hours=2))
    sent: list[uuid.UUID] = []

    async def _refused_again(invoice_id, org_id, actor_id, **_kwargs):
        sent.append(invoice_id)
        async with realdb.sessionmaker(TENANT)() as s:
            await s.execute(
                update(Invoice).where(Invoice.id == invoice_id).values(status=InvoiceStatus.failed)
            )
            await s.commit()
        await _refuse(
            realdb,
            invoice_id,
            at=datetime.now(UTC) + timedelta(seconds=1),
            retry_after=(datetime.now(UTC) + timedelta(hours=20)).isoformat(),
        )

    monkeypatch.setattr("app.services.erp_send.dispatch_erp", _refused_again)

    result = await sweep.run_budget_retry_sweep_once()

    assert sent == [earlier]
    assert result.invoices_resent == 1
    assert result.tenants_deferred == 1
    assert await _status(realdb, later) == InvoiceStatus.failed
    # Next tick: the one left behind is sent; the re-refused one is due again
    # only at its new `retry_after`.
    await sweep.run_budget_retry_sweep_once()
    assert sent == [earlier, later]


async def test_a_paused_budget_sends_nothing(realdb, dispatched, monkeypatch):
    await _write_erp(realdb, LIVE_ERP)
    await _seed_refused(realdb)

    async def _paused(_org):
        return True

    monkeypatch.setattr(sweep, "_budget_paused", _paused)

    result = await sweep.run_budget_retry_sweep_once()

    assert dispatched == []
    assert result.tenants_deferred == 1


async def test_the_budget_pre_check_reads_the_connection_budget(realdb, monkeypatch):
    """`_budget_paused` asks the adapter's own call budget, read-only."""
    from app.services import erp_call_budget as budget_mod
    from app.services import provider_credentials

    paused = budget_mod.BudgetRefusal(budget_mod.BudgetReason.tenant_budget, _ago(hours=-5))
    usage = {"paused": paused}

    class _Budget:
        async def usage(self):
            return budget_mod.CallUsage(
                budget_mod.UsageScope.org_share, 10, 10, _ago(hours=-5), usage["paused"]
            )

    class _Adapter:
        def call_budget(self):
            return _Budget()

    async def _config(_org, _block, *, db=None):
        return {"type": "blackbaud_fe_nxt"}

    monkeypatch.setattr(provider_credentials, "provider_config", _config)
    monkeypatch.setattr("app.services.erp_adapters.get_erp_adapter", lambda _cfg: _Adapter())
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)

    assert await sweep._budget_paused(org) is True
    usage["paused"] = None
    assert await sweep._budget_paused(org) is False


# ---------------------------------------------------------------------------
# idempotent across replicas; a gate refusal is recorded once
# ---------------------------------------------------------------------------


async def test_two_replicas_at_once_send_once(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed_refused(realdb)

    first, second = await asyncio.gather(
        sweep.run_budget_retry_sweep_once(), sweep.run_budget_retry_sweep_once()
    )

    assert first.invoices_resent + second.invoices_resent == 1
    assert first.invoice_failures == second.invoice_failures == 0
    assert dispatched == [inv_id]
    assert len(await _audit(realdb, inv_id, "invoice.erp_retried")) == 1


async def test_a_gate_refusal_is_recorded_once_and_ends_the_candidacy(realdb, dispatched):
    """The plan gate refuses the push (§258: Merge.dev is a Scale feature, and
    this tenant is on Growth). The refusal is recorded once, even by two
    replicas, and the invoice is then a person's to decide."""
    await _write_erp(realdb, {"type": "netsuite", "integration_method": "merge_dev"})
    inv_id = await _seed_refused(realdb)

    first, second = await asyncio.gather(
        sweep.run_budget_retry_sweep_once(), sweep.run_budget_retry_sweep_once()
    )

    assert first.invoices_refused + second.invoices_refused == 1
    assert first.invoice_failures == second.invoice_failures == 0
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.failed
    [refused] = await _audit(realdb, inv_id, sweep.BUDGET_RESEND_REFUSED_ACTION)
    assert refused.details["refusal"]["code"] == "plan_feature_required"
    assert refused.details["trigger"] == sweep.BUDGET_RESEND_TRIGGER
    assert refused.actor_id is None

    third = await sweep.run_budget_retry_sweep_once()

    assert third.invoices_refused == 0
    assert len(await _audit(realdb, inv_id, sweep.BUDGET_RESEND_REFUSED_ACTION)) == 1


# ---------------------------------------------------------------------------
# off by default, and registered
# ---------------------------------------------------------------------------


async def test_the_sweep_does_nothing_when_off(monkeypatch):
    monkeypatch.setattr(settings, "erp_budget_retry_sweep_enabled", False)

    assert await sweep.run_budget_retry_sweep_once() == sweep.BudgetRetrySweepResult()


def test_the_sweep_defaults_off_and_is_registered_with_the_health_report():
    from pathlib import Path

    from app.services.sweep_health import SWEEP_ENABLED_FLAGS, SWEEP_ERP_BUDGET_RETRY

    assert Settings.model_fields["erp_budget_retry_sweep_enabled"].default is False
    assert SWEEP_ENABLED_FLAGS[SWEEP_ERP_BUDGET_RETRY] == "erp_budget_retry_sweep_enabled"
    dev_env = (Path(__file__).resolve().parents[1] / ".env.development").read_text()
    assert "FEOH_ERP_BUDGET_RETRY_SWEEP_ENABLED" not in dev_env
    main_src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert 'name="erp-budget-retry"' in main_src
