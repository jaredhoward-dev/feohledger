"""The Merge.dev bill read-back sweep (decisions §540).

The Merge webhook drops a delivery it cannot verify (credentials unopenable,
unseal budget spent for a cold tenant — §408, §525, §527). The sweep lists each
Merge tenant's changed bills since its cursor, keeps the ones we pushed, reads
each back and applies it with the webhook's own reconciler
(`erp_status_sync.apply_bill_report`).

Pinned here on real Postgres, with Merge's change feed and bill reads stubbed
on `MergeDevAdapter`:

* a void the webhook missed is flagged on its invoice, as the webhook would;
* an unchanged bill changes nothing; a bill nobody pushed is not read;
* two ticks, and two replicas at once, flag it once;
* the cursor moves only past bills applied: a failed read keeps the bill
  pending, a capped batch keeps the rest pending, and the next listing asks
  from where the last one started;
* a paused or refusing call budget reads nothing and moves nothing;
* a tenant whose credentials cannot be opened is skipped, logged without the
  exception's text, and the others carry on;
* off by default, and registered with the health report and the app startup.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import Settings, settings
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services import merge_readback_sweep as sweep
from app.services.erp_adapters.base import ErpCallBudgetExhausted
from app.services.erp_adapters.merge_dev import MergeDevAdapter, merge_bill_report

TENANT = "a"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "merge_readback_sweep_enabled", True)
    monkeypatch.setattr(settings, "merge_readback_sweep_batch_size", 50)
    monkeypatch.setattr(settings, "merge_readback_sweep_lookback_hours", 48)
    monkeypatch.setattr(settings, "merge_readback_sweep_overlap_minutes", 10)


def _bill(bill_id: str, status: str = "OPEN") -> dict:
    return {
        "id": bill_id,
        "type": "ACCOUNTS_PAYABLE",
        "status": status,
        "payments": [],
        "applied_payments": [],
        "applied_credit_notes": [],
        "applied_vendor_credits": [],
        "remote_was_deleted": False,
    }


@pytest.fixture
def merge(monkeypatch):
    """Merge's side: ``feed`` is what the change feed lists, ``bills`` what a
    read answers (a dict, or an exception to raise); ``since`` and ``read``
    record the calls."""

    class _Merge:
        feed: list[str] = []
        bills: dict[str, object] = {}
        since: list[datetime] = []
        read: list[str] = []

    m = _Merge()
    m.feed, m.bills, m.since, m.read = [], {}, [], []

    async def _changed(self, since):
        m.since.append(since)
        return list(m.feed)

    async def _report(self, bill_id):
        m.read.append(bill_id)
        value = m.bills.get(bill_id, _bill(bill_id))
        if isinstance(value, BaseException):
            raise value
        return merge_bill_report(value)

    monkeypatch.setattr(MergeDevAdapter, "changed_bills", _changed)
    monkeypatch.setattr(MergeDevAdapter, "bill_report", _report)
    return m


async def _merge_tenant(realdb) -> None:
    info = realdb.info(TENANT)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        org.settings = {
            **{k: v for k, v in (org.settings or {}).items() if k != sweep.CURSOR_KEY},
            "erp": {"integration_method": "merge_dev"},
        }
        flag_modified(org, "settings")
        await s.commit()
    await realdb.store_provider_secrets(
        TENANT, "erp", {"api_key": "k", "account_token": "t", "webhook_signing_secret": "w"}
    )


async def _pushed(realdb, status: InvoiceStatus = InvoiceStatus.done) -> tuple[uuid.UUID, str]:
    """An invoice pushed to Merge as a fresh bill id; returns (invoice, bill)."""
    info = realdb.info(TENANT)
    inv_id, corr, bill = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    async with realdb.sessionmaker(TENANT)() as s:
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        s.add(
            Invoice(
                id=inv_id,
                correlation_id=corr,
                organization_id=info.org_id,
                invoice_number=f"MRB-{uuid.uuid4().hex[:8]}",
                vendor_name="Synced Vendor",
                amount=Decimal("250.00"),
                currency="USD",
                status=status,
                uploaded_by_id=None,  # seeded as already pushed; no employee
            )
        )
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=corr,
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": bill, "erp_realm": "merge_dev:"},
            )
        )
        await s.commit()
    return inv_id, bill


async def _exceptions(realdb, inv_id) -> list[APException]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(select(APException).where(APException.invoice_id == inv_id))
        return list(rows.scalars())


async def _cursor(realdb) -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        return dict((org.settings or {}).get(sweep.CURSOR_KEY) or {})


async def _resolve(realdb, inv_id) -> None:
    async with realdb.sessionmaker(TENANT)() as s:
        for row in (
            await s.execute(select(APException).where(APException.invoice_id == inv_id))
        ).scalars():
            row.status = "resolved"
        await s.commit()


# ---------------------------------------------------------------------------
# a missed void is applied through the webhook's reconciler
# ---------------------------------------------------------------------------


async def test_a_void_the_webhook_missed_is_flagged(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.feed = [bill]
    merge.bills[bill] = _bill(bill, "VOID")

    result = await sweep.run_merge_readback_once(now=NOW)

    assert result.failures == 0
    assert result.bills_applied == 1
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "erp_reconciliation"
    assert row.erp_report["erp_type"] == "merge_dev"
    assert row.erp_report["erp_document_id"] == bill
    assert row.erp_report["source"] == "reconciliation_sweep"
    # A first pass asks for the lookback window.
    assert merge.since == [NOW - timedelta(hours=48)]
    assert await _cursor(realdb) == {"through": NOW.isoformat(), "pending": []}


async def test_a_bill_deleted_in_the_erp_is_flagged(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb, InvoiceStatus.approved)
    merge.feed = [bill]
    merge.bills[bill] = {**_bill(bill), "remote_was_deleted": True}

    await sweep.run_merge_readback_once(now=NOW)

    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "payment_reconciliation"


async def test_an_unchanged_bill_changes_nothing(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.feed = [bill]

    result = await sweep.run_merge_readback_once(now=NOW)

    assert merge.read == [bill]
    assert result.bills_applied == 0
    assert await _exceptions(realdb, inv_id) == []


async def test_a_bill_nobody_pushed_is_not_read(realdb, merge):
    await _merge_tenant(realdb)
    merge.feed = [str(uuid.uuid4())]

    result = await sweep.run_merge_readback_once(now=NOW)

    assert result.bills_listed == 1
    assert merge.read == []


async def test_a_bill_merge_no_longer_has_is_not_read_as_gone(realdb, merge):
    from app.services.erp_adapters.merge_dev import MergeBillNotFound

    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.feed = [bill]
    merge.bills[bill] = MergeBillNotFound(bill)

    result = await sweep.run_merge_readback_once(now=NOW)

    assert result.bills_not_found == 1
    assert await _exceptions(realdb, inv_id) == []
    assert (await _cursor(realdb))["pending"] == []


# ---------------------------------------------------------------------------
# idempotent: two ticks, two replicas
# ---------------------------------------------------------------------------


async def test_two_ticks_flag_once_and_the_second_asks_from_the_first(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.feed = [bill]
    merge.bills[bill] = _bill(bill, "VOID")

    await sweep.run_merge_readback_once(now=NOW)
    merge.feed = []  # Merge has nothing new since the first listing
    later = NOW + timedelta(minutes=15)
    await sweep.run_merge_readback_once(now=later)

    # The second listing asks from the first's start less the overlap (§544).
    assert merge.since == [NOW - timedelta(hours=48), NOW - timedelta(minutes=10)]
    assert len(await _exceptions(realdb, inv_id)) == 1
    assert (await _cursor(realdb))["through"] == later.isoformat()


async def test_a_bill_stamped_just_before_the_cursor_is_still_read(realdb, merge, monkeypatch):
    """The stored mark is OUR clock at the last listing's start. A bill whose
    change Merge stamped a little before it (clock skew, or Merge's own lag
    syncing the ERP) but did not list in time must still be read: the next
    listing asks from the mark less the overlap (decisions §544)."""
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.bills[bill] = _bill(bill, "VOID")
    # Merge's feed honours `since`: it lists a bill only if Merge stamped it
    # after the asked moment. This change is stamped 3 minutes before our mark
    # and only became visible to Merge's listing after the first tick.
    stamped = NOW - timedelta(minutes=3)
    visible: dict[str, datetime] = {}

    async def _changed(self, since):
        merge.since.append(since)
        return [b for b, at in visible.items() if at > since]

    monkeypatch.setattr(MergeDevAdapter, "changed_bills", _changed)

    await sweep.run_merge_readback_once(now=NOW)
    assert merge.read == [] and await _exceptions(realdb, inv_id) == []

    visible[bill] = stamped
    await sweep.run_merge_readback_once(now=NOW + timedelta(minutes=15))

    assert merge.read == [bill]
    [row] = await _exceptions(realdb, inv_id)
    assert row.erp_report["erp_document_id"] == bill


async def test_with_no_overlap_the_late_stamped_bill_would_be_missed(realdb, merge, monkeypatch):
    """The control for the test above: the margin is what reads it."""
    monkeypatch.setattr(settings, "merge_readback_sweep_overlap_minutes", 0)
    await _merge_tenant(realdb)
    _inv_id, bill = await _pushed(realdb)
    visible: dict[str, datetime] = {}

    async def _changed(self, since):
        merge.since.append(since)
        return [b for b, at in visible.items() if at > since]

    monkeypatch.setattr(MergeDevAdapter, "changed_bills", _changed)

    await sweep.run_merge_readback_once(now=NOW)
    visible[bill] = NOW - timedelta(minutes=3)
    await sweep.run_merge_readback_once(now=NOW + timedelta(minutes=15))

    assert merge.read == []


async def test_the_overlap_defaults_to_ten_minutes():
    assert Settings.model_fields["merge_readback_sweep_overlap_minutes"].default == 10


async def test_a_resolved_flag_is_not_reopened_by_the_next_tick(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.feed = [bill]
    merge.bills[bill] = _bill(bill, "VOID")
    await sweep.run_merge_readback_once(now=NOW)
    await _resolve(realdb, inv_id)

    merge.feed = []
    await sweep.run_merge_readback_once(now=NOW + timedelta(minutes=15))

    assert [r.status for r in await _exceptions(realdb, inv_id)] == ["resolved"]


async def test_two_replicas_at_once_flag_once(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.feed = [bill]
    merge.bills[bill] = _bill(bill, "VOID")

    first, second = await asyncio.gather(
        sweep.run_merge_readback_once(now=NOW), sweep.run_merge_readback_once(now=NOW)
    )

    assert first.failures == second.failures == 0
    assert len(await _exceptions(realdb, inv_id)) == 1
    assert await _cursor(realdb) == {"through": NOW.isoformat(), "pending": []}


# ---------------------------------------------------------------------------
# the cursor moves only past what was applied
# ---------------------------------------------------------------------------


async def test_a_failed_read_keeps_the_bill_pending_and_lists_nothing_new(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, bill = await _pushed(realdb)
    merge.feed = [bill]
    merge.bills[bill] = RuntimeError("HTTP 502")

    result = await sweep.run_merge_readback_once(now=NOW)

    assert result.failures == 1
    assert await _exceptions(realdb, inv_id) == []
    assert await _cursor(realdb) == {"through": NOW.isoformat(), "pending": [bill]}

    # Next tick: the pending bill is read again before any new listing.
    merge.bills[bill] = _bill(bill, "VOID")
    await sweep.run_merge_readback_once(now=NOW + timedelta(minutes=15))
    assert len(merge.since) == 1
    assert len(await _exceptions(realdb, inv_id)) == 1
    assert (await _cursor(realdb))["pending"] == []


async def test_a_capped_batch_keeps_the_rest_pending(realdb, merge, monkeypatch):
    monkeypatch.setattr(settings, "merge_readback_sweep_batch_size", 1)
    await _merge_tenant(realdb)
    a_inv, a = await _pushed(realdb)
    b_inv, b = await _pushed(realdb)
    merge.feed = [a, b]
    merge.bills[a] = _bill(a, "VOID")
    merge.bills[b] = _bill(b, "VOID")

    await sweep.run_merge_readback_once(now=NOW)
    assert merge.read == [a]
    assert (await _cursor(realdb))["pending"] == [b]

    await sweep.run_merge_readback_once(now=NOW + timedelta(minutes=15))
    assert merge.read == [a, b]
    assert len(merge.since) == 1
    assert len(await _exceptions(realdb, a_inv)) == len(await _exceptions(realdb, b_inv)) == 1


async def test_a_failed_listing_moves_nothing(realdb, merge, monkeypatch):
    from app.services.erp_adapters.base import ErpListingIncomplete, ListingGap

    await _merge_tenant(realdb)

    async def _refused(self, since):
        raise ErpListingIncomplete("merge_dev", "invoices", ListingGap.fetch_failed)

    monkeypatch.setattr(MergeDevAdapter, "changed_bills", _refused)
    result = await sweep.run_merge_readback_once(now=NOW)

    assert result.failures == 1
    assert await _cursor(realdb) == {}


# ---------------------------------------------------------------------------
# budget, credentials
# ---------------------------------------------------------------------------


async def test_a_paused_budget_reads_nothing(realdb, merge, monkeypatch):
    await _merge_tenant(realdb)
    _, bill = await _pushed(realdb)
    merge.feed = [bill]

    async def _paused(adapter):
        return True

    monkeypatch.setattr(sweep, "_budget_paused", _paused)
    result = await sweep.run_merge_readback_once(now=NOW)

    assert result.tenants_deferred == 1
    assert merge.since == [] and merge.read == []
    assert await _cursor(realdb) == {}


async def test_a_read_the_budget_refuses_ends_the_pass_and_keeps_it_pending(realdb, merge):
    await _merge_tenant(realdb)
    inv_id, a = await _pushed(realdb)
    _, b = await _pushed(realdb)
    merge.feed = [a, b]
    merge.bills[a] = ErpCallBudgetExhausted("merge_dev", "tenant_budget", NOW)

    result = await sweep.run_merge_readback_once(now=NOW)

    assert result.tenants_deferred == 1
    assert merge.read == [a]
    assert (await _cursor(realdb))["pending"] == [a, b]
    assert await _exceptions(realdb, inv_id) == []


async def test_a_tenant_whose_credentials_cannot_be_opened_is_skipped(
    realdb, merge, monkeypatch, caplog
):
    from app.services import provider_credentials
    from app.services.credential_crypto import CredentialCryptoError

    await _merge_tenant(realdb)
    _, bill = await _pushed(realdb)
    merge.feed = [bill]

    async def _sealed(org, block, *, db=None):
        raise CredentialCryptoError("kms unavailable")

    monkeypatch.setattr(provider_credentials, "provider_config", _sealed)
    with caplog.at_level(logging.ERROR, logger=sweep.logger.name):
        result = await sweep.run_merge_readback_once(now=NOW)

    assert result.failures >= 1
    assert merge.since == [] and merge.read == []
    assert "could not be opened" in caplog.text
    assert "kms unavailable" not in caplog.text
    assert await _cursor(realdb) == {}


# ---------------------------------------------------------------------------
# off by default, registered
# ---------------------------------------------------------------------------


async def test_the_sweep_does_nothing_when_off(monkeypatch):
    monkeypatch.setattr(settings, "merge_readback_sweep_enabled", False)
    assert await sweep.run_merge_readback_once() == sweep.MergeReadbackResult()


def test_the_sweep_defaults_off_and_is_registered():
    from app import main
    from app.services.sweep_health import SWEEP_ENABLED_FLAGS, SWEEP_MERGE_READBACK

    assert Settings.model_fields["merge_readback_sweep_enabled"].default is False
    assert SWEEP_ENABLED_FLAGS[SWEEP_MERGE_READBACK] == "merge_readback_sweep_enabled"
    source = inspect.getsource(main)
    assert "run_merge_readback_loop()" in source
    assert "merge_readback_task," in source
