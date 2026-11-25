"""The ERP status polling sweep (decisions §528).

The generic per-tenant ERP webhook drops a delivery it cannot verify
(credentials unopenable, unseal budget spent — §408, §525). The sweep asks the
tenant's adapter for each sent bill's status and applies a change through the
webhook's own transition (`services/erp_status_report`).

Pinned here, on real Postgres, with the mock adapter (the local default):

* a missed `open` moves `sent_to_erp → posted_in_erp` with the webhook's audit
  action, marked `source: poll`; a missed `paid` on a scheduled payment moves
  it to `paid`;
* an unchanged status writes nothing to the trail, and a void after posting
  opens one exception that a later tick does not re-open after it is resolved;
* a bill the ERP shows settled with no payment behind it (§303) leaves the
  invoice where it is and opens one payment-blocking `payment_reconciliation`
  exception, raised again only after the status changes away and back (§532);
* two replicas at once apply once;
* a tenant whose ERP credentials cannot be opened is logged and skipped;
* the call budget ends a tenant's pass (pre-check, and a read it refuses);
* a bill pushed before the ERP went live, a closed invoice, a QuickBooks
  tenant are not polled;
* off by default, and registered with the health report.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.config import Settings, settings
from app.models.exception import Exception as ApException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.workflow import AuditLog, WorkflowInstance
from app.services import erp_status_poll_sweep as sweep
from app.services.erp_adapters.base import ErpCallBudgetExhausted, ErpInvoiceStatus
from app.services.erp_adapters.mock_adapter import MockAdapter

TENANT = "a"
MOCK_ERP = {"type": "mock", "integration_method": "direct"}

pytestmark = pytest.mark.plan("growth")


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "erp_status_poll_sweep_enabled", True)
    monkeypatch.setattr(settings, "erp_status_poll_sweep_batch_size", 50)


@pytest.fixture
def erp_reads(monkeypatch):
    """What the mock ERP reports per document id (``open`` by default), and
    the ids it was asked about."""
    reads: dict[str, ErpInvoiceStatus | Exception] = {}
    asked: list[str] = []

    async def _status(self, erp_document_id):
        asked.append(erp_document_id)
        value = reads.get(erp_document_id, ErpInvoiceStatus.open)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(MockAdapter, "get_invoice_status", _status)
    return reads, asked


async def _write_erp(realdb, erp: dict) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        org.settings = {**(org.settings or {}), "erp": erp}
        flag_modified(org, "settings")
        await s.commit()


async def _seed(realdb, status: InvoiceStatus, *, doc: str | None = None) -> uuid.UUID:
    from app.services.workflow_engine import get_or_create_workflow_definition

    info = realdb.info(TENANT)
    inv_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=info.org_id,
            invoice_number=f"POLL-{inv_id.hex[:8]}",
            vendor_name="Northwind Supplies",
            amount=Decimal("1234.56"),
            currency="USD",
            status=status,
            approved_by="Pat Manager",
            uploaded_by_id=None,
            erp_document_id=doc or f"BILL-{inv_id.hex[:8]}",
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
                state="completed",
                state_data={"erp_reference": inv.erp_document_id},
                steps_config_snapshot={"steps": []},
            )
        )
        s.add(
            AuditLog(
                correlation_id=inv.correlation_id,
                organization_id=info.org_id,
                actor_id=None,
                action=sweep.ERP_CONFIRMED_ACTION,
                entity_type="invoice",
                entity_id=inv_id,
                details={},
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await s.commit()
    return inv_id


async def _doc(realdb, inv_id) -> str:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(Invoice, inv_id)).erp_document_id


async def _status(realdb, inv_id) -> InvoiceStatus:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(Invoice, inv_id)).status


async def _audit(realdb, inv_id, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == inv_id, AuditLog.action == action)
        )
        return list(rows.scalars())


async def _erp_status_rows(realdb, inv_id) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(AuditLog).where(
                AuditLog.entity_id == inv_id, AuditLog.action.like("invoice.erp_status_%")
            )
        )
        return list(rows.scalars())


async def _exceptions(realdb, inv_id) -> list[ApException]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(select(ApException).where(ApException.invoice_id == inv_id))
        return list(rows.scalars())


# ---------------------------------------------------------------------------
# a missed status is applied, through the webhook's transition
# ---------------------------------------------------------------------------


async def test_a_missed_open_moves_sent_to_erp_to_posted(realdb, erp_reads):
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.sent_to_erp)

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1
    assert result.failures == 0 and result.invoice_failures == 0
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp
    [row] = await _audit(realdb, inv_id, "invoice.erp_status_posted_in_erp")
    assert row.actor_id is None
    assert row.details["source"] == "poll"
    assert row.details["erp_status"] == "posted"
    assert row.details["erp_type"] == "mock"
    assert row.details["erp_document_id"] == await _doc(realdb, inv_id)


async def test_a_missed_paid_moves_a_scheduled_payment_to_paid(realdb, erp_reads):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.paid

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.paid
    assert len(await _audit(realdb, inv_id, "invoice.erp_status_paid")) == 1


# ---------------------------------------------------------------------------
# unchanged is a no-op; a void is flagged once
# ---------------------------------------------------------------------------


async def test_an_unchanged_status_writes_nothing_to_the_trail(realdb, erp_reads):
    _, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)

    first = await sweep.run_status_poll_sweep_once()
    second = await sweep.run_status_poll_sweep_once()

    assert first.invoices_unchanged == 1 and second.invoices_unchanged == 1
    assert first.invoices_applied == second.invoices_applied == 0
    assert asked == [await _doc(realdb, inv_id)] * 2
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp
    assert await _erp_status_rows(realdb, inv_id) == []
    async with realdb.sessionmaker(TENANT)() as s:
        instance = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
    assert instance.state_data[sweep.STATE_KEY]["status"] == "open"
    assert instance.state_data["erp_reference"] == await _doc(realdb, inv_id)


async def test_a_void_after_posting_opens_one_exception_not_reopened_once_resolved(
    realdb, erp_reads
):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.cancelled

    first = await sweep.run_status_poll_sweep_once()
    assert first.invoices_applied == 1
    [exc] = await _exceptions(realdb, inv_id)
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp

    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(ApException).where(ApException.id == exc.id).values(status="resolved")
        )
        await s.commit()

    second = await sweep.run_status_poll_sweep_once()
    assert second.invoices_applied == 0 and second.invoices_unchanged == 1
    assert len(await _exceptions(realdb, inv_id)) == 1


async def test_a_status_with_no_webhook_equivalent_is_not_applied(realdb, erp_reads):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.partially_paid

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 0 and result.invoices_unchanged == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp
    assert await _exceptions(realdb, inv_id) == []


# ---------------------------------------------------------------------------
# settled with no payment behind it: flagged for a person, never paid (§303, §532)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [InvoiceStatus.posted_in_erp, InvoiceStatus.payment_scheduled])
async def test_settled_no_payment_opens_one_blocking_exception_and_leaves_the_invoice(
    realdb, erp_reads, status
):
    from app.services.payment_gates import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services.payment_runs import blocked_invoice_ids

    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, status)
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.settled_no_payment

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1 and result.invoice_failures == 0
    # Nothing was paid by us: no transition, no payment, no ERP-status audit row.
    assert await _status(realdb, inv_id) == status
    assert await _erp_status_rows(realdb, inv_id) == []
    async with realdb.sessionmaker(TENANT)() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.amount == Decimal("1234.56")
        assert (
            await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        ).scalars().all() == []
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.exception_type == "payment_reconciliation"
    assert exc.exception_type in PAYMENT_BLOCKING_EXCEPTION_TYPES
    assert exc.status == "open"
    assert exc.raised_by_user_id is None
    assert exc.description_code == "exception.erp_settled_no_payment"
    assert exc.erp_report == {
        "kind": "settled_no_payment",
        "erp_type": "mock",
        "erp_document_id": doc,
        "source": "poll",
        "invoice_status": status.value,
    }
    # The raise is on the append-only trail.
    assert len(await _audit(realdb, exc.id, "exception.raised")) == 1
    # A payment run refuses it.
    async with realdb.sessionmaker(TENANT)() as s:
        assert await blocked_invoice_ids(s, [inv_id]) == {inv_id}


async def test_settled_no_payment_is_not_reraised_on_the_next_tick(realdb, erp_reads):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.settled_no_payment

    await sweep.run_status_poll_sweep_once()
    second = await sweep.run_status_poll_sweep_once()

    assert second.invoices_applied == 0 and second.invoices_unchanged == 1
    [exc] = await _exceptions(realdb, inv_id)
    assert len(await _audit(realdb, exc.id, "exception.raised")) == 1


@pytest.mark.parametrize("closed_as", ["resolved", "dismissed"])
async def test_settled_no_payment_is_not_reraised_once_closed_until_the_status_changes(
    realdb, erp_reads, closed_as
):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.settled_no_payment

    await sweep.run_status_poll_sweep_once()
    [exc] = await _exceptions(realdb, inv_id)
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(ApException).where(ApException.id == exc.id).values(status=closed_as)
        )
        await s.commit()

    # Unchanged: a person's decision stands.
    unchanged = await sweep.run_status_poll_sweep_once()
    assert unchanged.invoices_applied == 0
    assert len(await _exceptions(realdb, inv_id)) == 1

    # The ERP reopens the bill, then settles it with no payment again: a new
    # report, flagged again.
    reads[doc] = ErpInvoiceStatus.open
    await sweep.run_status_poll_sweep_once()
    reads[doc] = ErpInvoiceStatus.settled_no_payment
    again = await sweep.run_status_poll_sweep_once()
    assert again.invoices_applied == 1
    rows = await _exceptions(realdb, inv_id)
    assert sorted(r.status for r in rows) == sorted([closed_as, "open"])
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp


# ---------------------------------------------------------------------------
# idempotent across replicas
# ---------------------------------------------------------------------------


async def test_two_replicas_at_once_apply_once(realdb, erp_reads):
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.sent_to_erp)

    first, second = await asyncio.gather(
        sweep.run_status_poll_sweep_once(), sweep.run_status_poll_sweep_once()
    )

    assert first.invoices_applied + second.invoices_applied == 1
    assert first.invoice_failures == second.invoice_failures == 0
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp
    assert len(await _audit(realdb, inv_id, "invoice.erp_status_posted_in_erp")) == 1


async def test_two_replicas_reading_a_void_open_one_exception(realdb, erp_reads):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.cancelled

    await asyncio.gather(sweep.run_status_poll_sweep_once(), sweep.run_status_poll_sweep_once())

    assert len(await _exceptions(realdb, inv_id)) == 1


# ---------------------------------------------------------------------------
# credentials, budget, scope
# ---------------------------------------------------------------------------


async def test_a_tenant_whose_credentials_cannot_be_opened_is_skipped(
    realdb, erp_reads, monkeypatch, caplog
):
    from app.services import provider_credentials
    from app.services.credential_crypto import CredentialCryptoError

    _, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.sent_to_erp)

    async def _sealed(org, block, *, db=None):
        raise CredentialCryptoError("kms unavailable")

    monkeypatch.setattr(provider_credentials, "provider_config", _sealed)
    with caplog.at_level(logging.ERROR, logger=sweep.logger.name):
        result = await sweep.run_status_poll_sweep_once()

    assert result.failures >= 1
    assert asked == []
    assert await _status(realdb, inv_id) == InvoiceStatus.sent_to_erp
    assert "could not be opened" in caplog.text
    assert "kms unavailable" not in caplog.text


async def test_a_paused_budget_reads_nothing(realdb, erp_reads, monkeypatch):
    _, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.sent_to_erp)

    async def _paused(adapter):
        return True

    monkeypatch.setattr(sweep, "_budget_paused", _paused)
    result = await sweep.run_status_poll_sweep_once()

    assert result.tenants_deferred == 1
    assert asked == []
    assert await _status(realdb, inv_id) == InvoiceStatus.sent_to_erp


async def test_a_read_the_budget_refuses_ends_the_pass(realdb, erp_reads):
    reads, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    first = await _seed(realdb, InvoiceStatus.sent_to_erp)
    second = await _seed(realdb, InvoiceStatus.sent_to_erp)
    refusal = ErpCallBudgetExhausted("mock", "tenant_budget", datetime.now(UTC))
    reads[await _doc(realdb, first)] = refusal
    reads[await _doc(realdb, second)] = refusal

    result = await sweep.run_status_poll_sweep_once()

    assert result.tenants_deferred == 1
    assert len(asked) == 1
    assert result.invoices_polled == 0


async def test_least_recently_polled_first_within_the_batch(realdb, erp_reads, monkeypatch):
    _, asked = erp_reads
    monkeypatch.setattr(settings, "erp_status_poll_sweep_batch_size", 1)
    await _write_erp(realdb, MOCK_ERP)
    a = await _seed(realdb, InvoiceStatus.posted_in_erp)
    b = await _seed(realdb, InvoiceStatus.posted_in_erp)

    await sweep.run_status_poll_sweep_once()
    await sweep.run_status_poll_sweep_once()

    assert sorted(asked) == sorted([await _doc(realdb, a), await _doc(realdb, b)])


async def test_a_bill_pushed_before_the_erp_went_live_is_not_polled(realdb, erp_reads):
    _, asked = erp_reads
    await _write_erp(realdb, {**MOCK_ERP, "configured_at": datetime.now(UTC).isoformat()})
    await _seed(realdb, InvoiceStatus.sent_to_erp)  # confirmed an hour ago

    result = await sweep.run_status_poll_sweep_once()

    assert asked == [] and result.invoices_polled == 0


async def test_closed_invoices_are_not_polled(realdb, erp_reads):
    _, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    for status in (InvoiceStatus.done, InvoiceStatus.paid, InvoiceStatus.approved):
        await _seed(realdb, status)

    await sweep.run_status_poll_sweep_once()

    assert asked == []


def test_adapters_with_their_own_status_path_are_not_polled():
    from app.services.erp_adapters.dispatcher import MERGE_DEV_ADAPTER_KEY

    class _StatusSync(MockAdapter):
        supports_status_sync = True

    assert sweep.pollable_adapter(MockAdapter({}), "mock")
    assert not sweep.pollable_adapter(_StatusSync({}), "quickbooks_online")
    assert not sweep.pollable_adapter(MockAdapter({}), MERGE_DEV_ADAPTER_KEY)
    assert not sweep.pollable_adapter(None, "netsuite")


# ---------------------------------------------------------------------------
# off by default
# ---------------------------------------------------------------------------


async def test_the_sweep_does_nothing_when_off(monkeypatch):
    monkeypatch.setattr(settings, "erp_status_poll_sweep_enabled", False)
    result = await sweep.run_status_poll_sweep_once()
    assert result == sweep.StatusPollSweepResult()


def test_the_sweep_defaults_off_and_is_registered_with_the_health_report():
    from app.services.sweep_health import SWEEP_ENABLED_FLAGS, SWEEP_ERP_STATUS_POLL

    assert Settings.model_fields["erp_status_poll_sweep_enabled"].default is False
    assert SWEEP_ENABLED_FLAGS[SWEEP_ERP_STATUS_POLL] == "erp_status_poll_sweep_enabled"
