"""The ERP status polling sweep (decisions §528).

The generic per-tenant ERP webhook drops a delivery it cannot verify
(credentials unopenable, unseal budget spent — §408, §525). The sweep asks the
tenant's adapter for each sent bill's status and applies a change through the
webhook's own transition (`services/erp_status_report`).

Pinned here, on real Postgres, against the real `mock` connection: its pushes
record the mock's own company (`erp_link_realm.MOCK_BILL_REALM`, §538), so it is
polled under the same realm rule as any real ERP. Only the legacy-realm cases
need a connection with a real 0115 snapshot, and borrow a `generic` one served
by the mock adapter:

* a missed `open` moves `sent_to_erp → posted_in_erp` with the webhook's audit
  action, marked `source: poll`; a missed `paid` on a scheduled payment moves
  it to `paid` only when our payment completed and covers it, holds behind the
  settled-short exception when it settled short, and holds behind a blocking
  `erp_paid_while_payment_in_flight` exception while it is still in flight (§533);
* an unchanged status writes nothing to the trail, and a void after posting
  opens one exception that a later tick does not re-open after it is resolved;
* a bill the ERP shows settled with no payment behind it (§303), or part paid
  (§533), leaves the invoice where it is and opens one payment-blocking
  `payment_reconciliation` exception, raised again only after the status
  changes away and back (§532) — and never swallowed by an unrelated live row
  of the same type (§533);
* the bills polled follow the webhook's by-document realm rule (§417, §423);
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
from app.services.erp_link_realm import MOCK_BILL_REALM

TENANT = "a"
MOCK_ERP = {"type": "mock", "integration_method": "direct"}
#: What a push through the mock records (§538).
REALM = MOCK_BILL_REALM
#: A directly connected ERP with a real realm (``generic:``), for the legacy
#: (pre-realm) cases only; ``_served_by_mock`` answers its reads.
GENERIC_ERP = {"type": "generic", "integration_method": "direct"}
GENERIC_REALM = "generic:"

pytestmark = pytest.mark.plan("growth")


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "erp_status_poll_sweep_enabled", True)
    monkeypatch.setattr(settings, "erp_status_poll_sweep_batch_size", 50)


@pytest.fixture
def _served_by_mock(monkeypatch):
    """The ``generic`` ERP has no adapter in the registry; serve it with the
    mock adapter, so the realm-named connection is polled like a real one."""
    from app.services import erp_adapters

    real = erp_adapters.get_erp_adapter

    def _get(config):
        if (config or {}).get("type") == "generic":
            return MockAdapter(config)
        return real(config)

    monkeypatch.setattr(erp_adapters, "get_erp_adapter", _get)


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


_UNSET = object()


async def _seed(
    realdb, status: InvoiceStatus, *, doc: str | None = None, realm=_UNSET
) -> uuid.UUID:
    """An invoice whose confirmed push recorded ``realm`` (default: the
    connection's own; ``None``: a push from before realms were recorded)."""
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
                state_data={
                    "erp_reference": inv.erp_document_id,
                    **(
                        {"erp_realm": REALM if realm is _UNSET else realm}
                        if realm is not None
                        else {}
                    ),
                },
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


async def _unrelated_exception(realdb, inv_id, exception_type: str) -> uuid.UUID:
    """A live exception of ``exception_type`` on the invoice that some other
    report opened (a QuickBooks-shaped `bill_state` row)."""
    exc_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            ApException(
                id=exc_id,
                organization_id=realdb.info(TENANT).org_id,
                invoice_id=inv_id,
                exception_type=exception_type,
                severity="error",
                status="open",
                description="Some other reconciliation.",
                erp_report={"kind": "bill_state", "erp_type": "generic"},
            )
        )
        await s.commit()
    return exc_id


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


async def _payment(realdb, inv_id, *, status: str, settled: str | None = None) -> uuid.UUID:
    """A FeohLedger payment of the invoice's full amount; ``settled`` is what
    the rail reported moving (None: nothing reported yet)."""
    pay_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("1234.56"),
                method="ach",
                status=status,
                settled_amount=Decimal(settled) if settled is not None else None,
                settled_currency="USD" if settled is not None else None,
            )
        )
        await s.commit()
    return pay_id


async def test_a_missed_paid_moves_a_scheduled_payment_to_paid_once_ours_completed(
    realdb, erp_reads
):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    await _payment(realdb, inv_id, status="completed", settled="1234.56")
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.paid

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.paid
    assert len(await _audit(realdb, inv_id, "invoice.erp_status_paid")) == 1
    assert await _exceptions(realdb, inv_id) == []


async def test_a_missed_paid_over_a_short_settlement_is_held(realdb, erp_reads):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    pay_id = await _payment(realdb, inv_id, status="completed", settled="1000.00")
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.paid

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled
    assert await _erp_status_rows(realdb, inv_id) == []
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.erp_report["kind"] == "settled_short"
    assert exc.erp_report["payment_id"] == str(pay_id)
    assert exc.erp_report["shortfall"] == "234.56"


@pytest.mark.parametrize("claim", ["payment_in_flight", "card_row_no_payment"])
async def test_a_paid_read_while_our_payment_is_in_flight_is_held_and_blocked(
    realdb, erp_reads, claim
):
    """The ERP shows the bill paid while no FeohLedger payment has completed.
    Never moved to `paid` — that could hide a double payment — but held behind
    a blocking exception (decisions §533). The hold keys on "no completed
    `Payment`" alone: `card_row_no_payment` pins only that an active card row
    (which books no `Payment`) does not change that, not that the card is
    recognised as a claim (§535)."""
    from app.models.virtual_card import VirtualCard
    from app.services.payment_gates import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services.payment_runs import blocked_invoice_ids

    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    if claim == "payment_in_flight":
        await _payment(realdb, inv_id, status="processing")
    else:  # card_row_no_payment
        async with realdb.sessionmaker(TENANT)() as s:
            s.add(
                VirtualCard(
                    invoice_id=inv_id,
                    organization_id=realdb.info(TENANT).org_id,
                    card_provider="mock",
                    provider_card_id=f"card-{inv_id.hex[:8]}",
                    amount_limit=Decimal("1234.56"),
                    currency="USD",
                    status="active",
                )
            )
            await s.commit()
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.paid

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled
    assert await _erp_status_rows(realdb, inv_id) == []
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.exception_type == "payment_reconciliation"
    assert exc.exception_type in PAYMENT_BLOCKING_EXCEPTION_TYPES
    assert exc.description_code == "exception.erp_paid_while_payment_in_flight"
    assert exc.raised_by_user_id is None
    assert exc.erp_report == {
        "kind": "paid_while_scheduled",
        "erp_type": "mock",
        "erp_document_id": doc,
        "event_id": None,
        "invoice_status": "payment_scheduled",
        "source": "poll",
    }
    async with realdb.sessionmaker(TENANT)() as s:
        assert await blocked_invoice_ids(s, [inv_id]) == {inv_id}

    # Unchanged on the next tick: not raised again.
    again = await sweep.run_status_poll_sweep_once()
    assert again.invoices_applied == 0
    assert len(await _exceptions(realdb, inv_id)) == 1


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


@pytest.mark.parametrize("read", [ErpInvoiceStatus.draft, ErpInvoiceStatus.unknown])
async def test_a_status_with_no_webhook_equivalent_is_not_applied(realdb, erp_reads, read):
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    reads[await _doc(realdb, inv_id)] = read

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 0 and result.invoices_unchanged == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp
    assert await _exceptions(realdb, inv_id) == []


async def test_a_cancelled_read_is_not_swallowed_by_an_unrelated_open_row(realdb, erp_reads):
    """An unrelated live `erp_reconciliation` (say, a settled-short report) does
    not stand in for the void: the void's own row opens, so resolving the
    other row leaves the void in front of a person (decisions §533)."""
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    unrelated = await _unrelated_exception(realdb, inv_id, "erp_reconciliation")
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.cancelled

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1
    rows = {r.id: r for r in await _exceptions(realdb, inv_id)}
    assert len(rows) == 2
    [void] = [r for r in rows.values() if r.id != unrelated]
    assert void.erp_report["kind"] == "voided_after_post"


async def test_a_deduplicated_void_is_not_counted_as_applied(realdb, erp_reads):
    """A void whose own row is already live changes nothing: counted unchanged."""
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.cancelled
    await sweep.run_status_poll_sweep_once()
    # The ERP reopens it (the row stays open for a person), then voids it again.
    reads[doc] = ErpInvoiceStatus.open
    await sweep.run_status_poll_sweep_once()
    reads[doc] = ErpInvoiceStatus.cancelled

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 0 and result.invoices_unchanged == 1
    assert len(await _exceptions(realdb, inv_id)) == 1


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


async def test_settled_no_payment_is_not_swallowed_by_an_unrelated_blocking_row(realdb, erp_reads):
    """§532's dedupe was by type: a live `payment_reconciliation` some other
    report opened skipped the flag, the read was still recorded as handled, and
    once a person resolved the unrelated row nothing blocked the run for a bill
    the ERP shows settled. Deduplicated by kind, the flag opens beside it
    (decisions §533)."""
    from app.services.payment_runs import blocked_invoice_ids

    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    unrelated = await _unrelated_exception(realdb, inv_id, "payment_reconciliation")
    reads[await _doc(realdb, inv_id)] = ErpInvoiceStatus.settled_no_payment

    first = await sweep.run_status_poll_sweep_once()
    assert first.invoices_applied == 1

    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(ApException).where(ApException.id == unrelated).values(status="resolved")
        )
        await s.commit()

    await sweep.run_status_poll_sweep_once()

    async with realdb.sessionmaker(TENANT)() as s:
        assert await blocked_invoice_ids(s, [inv_id]) == {inv_id}
    live = [r for r in await _exceptions(realdb, inv_id) if r.status == "open"]
    assert [r.erp_report["kind"] for r in live] == ["settled_no_payment"]


@pytest.mark.parametrize(
    "status",
    [InvoiceStatus.sent_to_erp, InvoiceStatus.posted_in_erp, InvoiceStatus.payment_scheduled],
)
async def test_partially_paid_opens_one_blocking_exception_and_leaves_the_invoice(
    realdb, erp_reads, status
):
    """Part paid in the ERP, which this route never writes back to: a run for
    the full amount would overpay. Flagged as QuickBooks' sync flags it
    (`erp_partly_paid_outside`), the invoice not moved (decisions §533)."""
    from app.services.payment_runs import blocked_invoice_ids

    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, status)
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.partially_paid

    result = await sweep.run_status_poll_sweep_once()

    assert result.invoices_applied == 1 and result.invoice_failures == 0
    assert await _status(realdb, inv_id) == status
    assert await _erp_status_rows(realdb, inv_id) == []
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.exception_type == "payment_reconciliation"
    assert exc.description_code == "exception.erp_partly_paid_outside"
    assert exc.raised_by_user_id is None
    assert exc.erp_report == {
        "kind": "partly_paid",
        "erp_type": "mock",
        "erp_document_id": doc,
        "source": "poll",
        "invoice_status": status.value,
    }
    assert len(await _audit(realdb, exc.id, "exception.raised")) == 1
    async with realdb.sessionmaker(TENANT)() as s:
        assert await blocked_invoice_ids(s, [inv_id]) == {inv_id}

    # Once per change: an unchanged read raises nothing, even after a resolve.
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(ApException).where(ApException.id == exc.id).values(status="resolved")
        )
        await s.commit()
    again = await sweep.run_status_poll_sweep_once()
    assert again.invoices_applied == 0
    assert len(await _exceptions(realdb, inv_id)) == 1


async def test_partially_paid_beside_settled_no_payment_each_open_their_own(realdb, erp_reads):
    """Two different facts, two rows: the kind, not the type, deduplicates."""
    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.partially_paid
    await sweep.run_status_poll_sweep_once()
    reads[doc] = ErpInvoiceStatus.settled_no_payment
    await sweep.run_status_poll_sweep_once()

    kinds = sorted(r.erp_report["kind"] for r in await _exceptions(realdb, inv_id))
    assert kinds == ["partly_paid", "settled_no_payment"]


# ---------------------------------------------------------------------------
# which bills: the webhook's by-document realm rule (§417, §423, §533)
# ---------------------------------------------------------------------------


async def test_a_bill_the_mock_pushed_is_polled_under_its_own_company(realdb, erp_reads):
    """What a push through the mock records (``bill_realm``, pinned at the push
    in ``test_erp_push_flow.py``) is what the sweep polls under the one realm
    rule, with no adapter stand-in: guard rail 7 end to end (§538)."""
    from app.services.erp_link_realm import bill_realm

    _, asked = erp_reads
    assert bill_realm(MOCK_ERP) == REALM
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.sent_to_erp)

    result = await sweep.run_status_poll_sweep_once()

    assert asked == [await _doc(realdb, inv_id)] and result.invoices_polled == 1
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp


@pytest.mark.parametrize("legacy_realm", [None, MOCK_BILL_REALM])
async def test_a_pre_realm_bill_is_never_polled_on_the_mock(realdb, erp_reads, legacy_realm):
    """A NULL-realm bill on a mock tenant may be a real ERP's from before realms
    were recorded: the mock never trusts one, whatever the 0115 snapshot says
    (§538). Nor is a link stamp (``"mock"``, no ``:``) ever a bill's realm."""
    from app.services.erp_link_realm import LEGACY_REALM_KEY, UNNAMED_REALM

    _, asked = erp_reads
    erp = dict(MOCK_ERP)
    if legacy_realm is not None:
        erp[LEGACY_REALM_KEY] = legacy_realm
    await _write_erp(realdb, erp)
    await _seed(realdb, InvoiceStatus.sent_to_erp, realm=None)
    await _seed(realdb, InvoiceStatus.sent_to_erp, realm=UNNAMED_REALM)

    result = await sweep.run_status_poll_sweep_once()

    assert asked == [] and result.invoices_polled == 0


@pytest.mark.parametrize("pushed_realm", ["xero:tenant-1", "generic:", "mock:other-company"])
async def test_a_bill_another_realm_issued_is_never_polled(realdb, erp_reads, pushed_realm):
    _, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    await _seed(realdb, InvoiceStatus.sent_to_erp, realm=pushed_realm)

    await sweep.run_status_poll_sweep_once()

    assert asked == []


@pytest.mark.parametrize(
    ("legacy_realm", "polled"),
    [(None, False), ("netsuite:1234567", False), (GENERIC_REALM, True)],
)
async def test_a_pre_realm_bill_is_polled_only_while_its_legacy_realm_is_trusted(
    realdb, erp_reads, _served_by_mock, legacy_realm, polled
):
    from app.services.erp_link_realm import LEGACY_REALM_KEY

    _, asked = erp_reads
    erp = dict(GENERIC_ERP)
    if legacy_realm is not None:
        erp[LEGACY_REALM_KEY] = legacy_realm
    await _write_erp(realdb, erp)
    inv_id = await _seed(realdb, InvoiceStatus.sent_to_erp, realm=None)

    await sweep.run_status_poll_sweep_once()

    assert asked == ([await _doc(realdb, inv_id)] if polled else [])


def test_the_sweep_and_the_webhook_share_one_realm_rule():
    """No restated copy: both call `erp_link_realm.bill_realm_clause`."""
    import inspect

    from app.api import erp_webhook

    assert "bill_realm_clause" in inspect.getsource(sweep._candidates_query)
    assert "bill_realm_clause" in inspect.getsource(erp_webhook._invoice_id_by_document)
    for reader in (sweep._candidates_query, erp_webhook._invoice_id_by_document):
        assert "legacy_links_trusted(" not in inspect.getsource(reader)


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


async def _card(realdb, inv_id) -> None:
    from app.models.virtual_card import VirtualCard

    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            VirtualCard(
                invoice_id=inv_id,
                organization_id=realdb.info(TENANT).org_id,
                card_provider="mock",
                provider_card_id=f"card-{inv_id.hex[:8]}",
                amount_limit=Decimal("1234.56"),
                currency="USD",
                status="active",
            )
        )
        await s.commit()


async def test_a_paid_read_for_a_voided_back_approved_invoice_is_recorded(realdb, erp_reads):
    """A payment void returns the invoice to `approved` with its ERP bill and
    realm intact. The ERP then shows the bill paid (the webhook dropped): the
    sweep reads it and records the ERP's payment, so no run pays it again
    (decisions §535)."""
    from app.models.invoice_status import PAYABLE_STATUSES

    reads, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.approved)
    await _payment(realdb, inv_id, status="voided")
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.paid

    result = await sweep.run_status_poll_sweep_once()

    assert doc in asked
    assert result.invoices_applied == 1 and result.invoice_failures == 0
    assert await _status(realdb, inv_id) == InvoiceStatus.paid
    assert InvoiceStatus.paid not in PAYABLE_STATUSES
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(Payment).where(Payment.invoice_id == inv_id, Payment.status == "completed")
        )
        [recorded] = list(rows.scalars())
    assert recorded.provider == "external"


async def test_a_paid_read_for_an_approved_invoice_with_a_live_card_is_refused_and_blocked(
    realdb, erp_reads
):
    """The same read while a live card still holds the invoice: recording it
    is refused (the card could still be charged), and the refusal is the
    payment-BLOCKING row the run builder refuses on (§319, §535)."""
    from app.services.payment_runs import blocked_invoice_ids

    reads, _ = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.approved)
    await _card(realdb, inv_id)
    doc = await _doc(realdb, inv_id)
    reads[doc] = ErpInvoiceStatus.paid

    await sweep.run_status_poll_sweep_once()

    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.exception_type == "payment_reconciliation"
    assert exc.erp_report["kind"] == "paid_not_recorded"
    assert exc.erp_report["refusal_code"] == "external_payment_card_live"
    async with realdb.sessionmaker(TENANT)() as s:
        assert await blocked_invoice_ids(s, [inv_id]) == {inv_id}


async def test_an_approved_invoice_never_pushed_is_not_polled(realdb, erp_reads):
    """`approved` is polled only for a bill in the ERP: a direct-schedule
    invoice with no document id is never read."""
    _, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    inv_id = await _seed(realdb, InvoiceStatus.approved)
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(update(Invoice).where(Invoice.id == inv_id).values(erp_document_id=None))
        await s.commit()

    await sweep.run_status_poll_sweep_once()

    assert asked == []


async def test_closed_invoices_are_not_polled(realdb, erp_reads):
    _, asked = erp_reads
    await _write_erp(realdb, MOCK_ERP)
    for status in (InvoiceStatus.done, InvoiceStatus.paid, InvoiceStatus.failed):
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
