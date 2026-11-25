"""The shared ERP status transition (`services/erp_status_report`).

The generic ERP webhook and the status polling sweep both end in
`apply_erp_status_report` (decisions §528), so this pins the service itself,
on real Postgres, apart from either channel:

* each status the generic webhook speaks → the edge it takes, through
  `transition_invoice` (audit row, `source` marked only when given);
* a forbidden edge is a no-op, never a 409; a terminal invoice is never moved;
* a void after posting opens one `erp_reconciliation` row, not a transition;
* `payment_scheduled` + `paid`: moves only over a completed payment that
  covers the invoice; a short settlement is held (settled-short row); no
  completed payment — ours still in flight, or none on file — is held behind
  one blocking `payment_reconciliation` (decisions §533, §535);
* a run's virtual-card leg (a `completed` payment booked at mint) counts only
  what its card was charged: uncharged → the in-flight hold, card untouched;
  charged in full → `paid`; charged short → settled-short hold (§537);
* every flag is deduplicated by its kind: a live row of the same kind
  suppresses it (and the outcome is `NOOP`, so nothing is counted or
  committed), an unrelated live row of the same type never does (§533);
* a non-`paid` status withdraws an unrecorded ERP payment (§319).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.models.exception import Exception as ApException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.workflow import AuditLog, WorkflowInstance
from app.services import erp_status_report as report
from app.services.erp_status_report import ERP_STATUS_MAP, apply_erp_status_report

TENANT = "a"
ERP_TYPE = "generic"


async def _seed(realdb, status: InvoiceStatus) -> uuid.UUID:
    from app.services.workflow_engine import get_or_create_workflow_definition

    info = realdb.info(TENANT)
    inv_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=info.org_id,
            invoice_number=f"RPT-{inv_id.hex[:8]}",
            vendor_name="Northwind Supplies",
            amount=Decimal("500.00"),
            currency="USD",
            status=status,
            approved_by="Pat Manager",
            uploaded_by_id=None,
            erp_document_id=f"BILL-{inv_id.hex[:8]}",
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
        await s.commit()
    return inv_id


async def _payment(realdb, inv_id, *, status: str, settled: str | None = None) -> uuid.UUID:
    pay_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("500.00"),
                method="ach",
                status=status,
                settled_amount=Decimal(settled) if settled is not None else None,
                settled_currency="USD" if settled is not None else None,
            )
        )
        await s.commit()
    return pay_id


async def _exception(realdb, inv_id, exception_type: str, kind: str) -> uuid.UUID:
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
                description="Another report.",
                erp_report={"kind": kind, "erp_type": ERP_TYPE},
            )
        )
        await s.commit()
    return exc_id


async def _apply(realdb, inv_id, erp_status: str, *, source: str | None = None) -> str:
    """One report, applied the way both channels apply it: row-locked, then
    committed unless the outcome is `NOOP`."""
    from app.services.workflow_engine import get_invoice_for_update

    info = realdb.info(TENANT)
    async with realdb.sessionmaker(TENANT)() as db:
        invoice = await get_invoice_for_update(db, inv_id)
        outcome = await apply_erp_status_report(
            db,
            invoice,
            org_id=info.org_id,
            org=SimpleNamespace(id=info.org_id, settings={}),
            erp_type=ERP_TYPE,
            erp_status=erp_status,
            target_status=ERP_STATUS_MAP[erp_status],
            erp_document_id=invoice.erp_document_id,
            event_id=None,
            source=source,
        )
        if outcome != report.NOOP:
            await db.commit()
        else:
            await db.rollback()
    return outcome


async def _status(realdb, inv_id) -> InvoiceStatus:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(Invoice, inv_id)).status


async def _exceptions(realdb, inv_id) -> list[ApException]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(select(ApException).where(ApException.invoice_id == inv_id))
        return list(rows.scalars())


async def _erp_status_rows(realdb, inv_id) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(AuditLog).where(
                AuditLog.entity_id == inv_id, AuditLog.action.like("invoice.erp_status_%")
            )
        )
        return list(rows.scalars())


async def _blocked(realdb, inv_id) -> bool:
    from app.services.payment_runs import blocked_invoice_ids

    async with realdb.sessionmaker(TENANT)() as s:
        return inv_id in await blocked_invoice_ids(s, [inv_id])


# ---------------------------------------------------------------------------
# each status → its edge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "erp_status", "expected"),
    [
        (InvoiceStatus.sent_to_erp, "posted", InvoiceStatus.posted_in_erp),
        (InvoiceStatus.sent_to_erp, "OPEN", InvoiceStatus.posted_in_erp),
        (InvoiceStatus.sending_to_erp, "cancelled", InvoiceStatus.failed),
        (InvoiceStatus.sending_to_erp, "voided", InvoiceStatus.failed),
    ],
)
async def test_a_permitted_status_transitions_through_the_audited_chokepoint(
    realdb, current, erp_status, expected
):
    inv_id = await _seed(realdb, current)

    outcome = await _apply(realdb, inv_id, erp_status)

    assert outcome == report.TRANSITIONED
    assert await _status(realdb, inv_id) == expected
    [row] = await _erp_status_rows(realdb, inv_id)
    assert row.action == f"invoice.erp_status_{expected.value}"
    assert row.details["erp_status"] == erp_status
    assert row.details["erp_type"] == ERP_TYPE
    assert "source" not in row.details
    assert await _exceptions(realdb, inv_id) == []


async def test_a_poll_marks_its_audit_row(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.sent_to_erp)

    await _apply(realdb, inv_id, "posted", source="poll")

    [row] = await _erp_status_rows(realdb, inv_id)
    assert row.details["source"] == "poll"


async def test_an_unchanged_status_is_a_noop(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)

    assert await _apply(realdb, inv_id, "posted") == report.NOOP
    assert await _status(realdb, inv_id) == InvoiceStatus.posted_in_erp
    assert await _erp_status_rows(realdb, inv_id) == []


@pytest.mark.parametrize("current", [InvoiceStatus.done, InvoiceStatus.paid])
async def test_a_paid_report_never_moves_a_closed_invoice(realdb, current):
    inv_id = await _seed(realdb, current)

    assert await _apply(realdb, inv_id, "paid") == report.NOOP
    assert await _status(realdb, inv_id) == current
    assert await _exceptions(realdb, inv_id) == []


async def test_a_paid_report_for_an_invoice_never_paid_here_is_recorded(realdb, monkeypatch):
    """`posted_in_erp → paid` has no edge: the ERP's payment is recorded through
    the paid-outside service instead (issue #517), never a bare transition."""
    record = AsyncMock()
    monkeypatch.setattr(report, "_record_erp_reported_payment", record)
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)

    assert await _apply(realdb, inv_id, "paid") == report.PAYMENT_RECORDED
    record.assert_awaited_once()
    assert await _erp_status_rows(realdb, inv_id) == []


# ---------------------------------------------------------------------------
# a void after posting: flagged once per kind, never transitioned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current", [InvoiceStatus.posted_in_erp, InvoiceStatus.payment_scheduled])
async def test_a_void_after_posting_opens_one_row_and_dedupes_by_kind(realdb, current):
    inv_id = await _seed(realdb, current)

    assert await _apply(realdb, inv_id, "cancelled") == report.EXCEPTION_OPENED
    assert await _status(realdb, inv_id) == current
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.exception_type == "erp_reconciliation"
    assert exc.erp_report["kind"] == "voided_after_post"

    # The same kind live: nothing changes, so nothing to count or commit.
    assert await _apply(realdb, inv_id, "voided") == report.NOOP
    assert len(await _exceptions(realdb, inv_id)) == 1


async def test_a_void_is_not_swallowed_by_an_unrelated_row_of_the_same_type(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    await _exception(realdb, inv_id, "erp_reconciliation", "settled_short")

    assert await _apply(realdb, inv_id, "cancelled") == report.EXCEPTION_OPENED
    kinds = sorted(r.erp_report["kind"] for r in await _exceptions(realdb, inv_id))
    assert kinds == ["settled_short", "voided_after_post"]


# ---------------------------------------------------------------------------
# payment_scheduled + paid (decisions §533)
# ---------------------------------------------------------------------------


async def test_paid_over_a_completed_covering_payment_moves_to_paid(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    await _payment(realdb, inv_id, status="completed", settled="500.00")

    assert await _apply(realdb, inv_id, "paid") == report.TRANSITIONED
    assert await _status(realdb, inv_id) == InvoiceStatus.paid
    assert await _exceptions(realdb, inv_id) == []


async def test_paid_over_a_short_settlement_is_held_once(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    pay_id = await _payment(realdb, inv_id, status="completed", settled="400.00")

    assert await _apply(realdb, inv_id, "paid") == report.HELD_SETTLED_SHORT
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.erp_report["kind"] == "settled_short"
    assert exc.erp_report["payment_id"] == str(pay_id)
    assert exc.erp_report["shortfall"] == "100.00"

    assert await _apply(realdb, inv_id, "Paid") == report.NOOP
    assert len(await _exceptions(realdb, inv_id)) == 1


@pytest.mark.parametrize("claim", ["none", "pending", "processing", "card_row_no_payment"])
async def test_paid_with_no_completed_payment_is_held_behind_a_blocking_row(realdb, claim):
    """Our rail payment has not completed (or nothing at all is on file), yet
    the ERP shows the bill paid. Moving to `paid` would hide a possible double
    payment, so it is held.

    The hold keys on "no completed `Payment`" alone. `card_row_no_payment`
    pins only that an active `VirtualCard` row, which books no `Payment`,
    does not change that: the outcome is the same with or without the card,
    so this case does NOT show the card is recognised as a claim. Neither real
    card shape reaches this branch (decisions §535): a `/cards/generate` card
    leaves the invoice `approved` / `posted_in_erp`, where an ERP `paid` is
    refused `external_payment_card_live`; a run's card leg books a completed
    `Payment`, held below while its card is uncharged (decisions §537)."""
    from app.models.virtual_card import VirtualCard

    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    if claim in ("pending", "processing"):
        await _payment(realdb, inv_id, status=claim)
    elif claim == "card_row_no_payment":
        async with realdb.sessionmaker(TENANT)() as s:
            s.add(
                VirtualCard(
                    invoice_id=inv_id,
                    organization_id=realdb.info(TENANT).org_id,
                    card_provider="mock",
                    provider_card_id=f"card-{inv_id.hex[:8]}",
                    amount_limit=Decimal("500.00"),
                    currency="USD",
                    status="active",
                )
            )
            await s.commit()

    assert await _apply(realdb, inv_id, "paid") == report.HELD_PAID_WHILE_SCHEDULED
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled
    assert await _erp_status_rows(realdb, inv_id) == []
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.exception_type == "payment_reconciliation"
    assert exc.description_code == "exception.erp_paid_while_payment_in_flight"
    assert exc.raised_by_user_id is None
    assert exc.erp_report["kind"] == "paid_while_scheduled"
    assert await _blocked(realdb, inv_id)
    async with realdb.sessionmaker(TENANT)() as s:
        raised = await s.execute(
            select(AuditLog).where(
                AuditLog.entity_id == exc.id, AuditLog.action == "exception.raised"
            )
        )
        assert len(list(raised.scalars())) == 1

    # A second report while it is live changes nothing.
    assert await _apply(realdb, inv_id, "paidInFull") == report.NOOP
    assert len(await _exceptions(realdb, inv_id)) == 1


async def _card_leg(realdb, inv_id, *, card_status: str, charged: str | None) -> uuid.UUID:
    """A run's card leg as `_execute_single_payment` books it: a `completed`
    `virtual_card` payment with no settled figure, and the card it minted."""
    from app.models.virtual_card import VirtualCard

    pay_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                amount=Decimal("500.00"),
                method="virtual_card",
                status="completed",
                provider="mock",
            )
        )
        await s.flush()
        s.add(
            VirtualCard(
                invoice_id=inv_id,
                payment_id=pay_id,
                organization_id=realdb.info(TENANT).org_id,
                card_provider="mock",
                provider_card_id=f"card-{inv_id.hex[:8]}",
                amount_limit=Decimal("500.00"),
                amount_charged=Decimal(charged) if charged is not None else None,
                currency="USD",
                status=card_status,
            )
        )
        await s.commit()
    return pay_id


@pytest.mark.parametrize("card_status", ["created", "sent", "active"])
async def test_paid_over_an_uncharged_run_card_leg_is_held_and_the_card_left_alone(
    realdb, card_status
):
    """The ERP shows the bill paid, but our card leg's card was never charged:
    nothing of ours has moved, and the card is still spendable. Held behind
    the in-flight row — never `paid`, and the card is not touched (§537)."""
    from app.models.virtual_card import VirtualCard

    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    await _card_leg(realdb, inv_id, card_status=card_status, charged=None)

    assert await _apply(realdb, inv_id, "paid") == report.HELD_PAID_WHILE_SCHEDULED
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.exception_type == "payment_reconciliation"
    assert exc.description_code == "exception.erp_paid_while_payment_in_flight"
    assert exc.erp_report["kind"] == "paid_while_scheduled"
    assert await _blocked(realdb, inv_id)
    async with realdb.sessionmaker(TENANT)() as s:
        card = (
            await s.execute(select(VirtualCard).where(VirtualCard.invoice_id == inv_id))
        ).scalar_one()
        assert card.status == card_status


async def test_paid_over_a_run_card_leg_only_authorized_is_held(realdb):
    """Decisions §543: authorized in full (`charged`) is not money moved — the
    authorization can still be reversed or expire — so an ERP `paid` is held
    behind the in-flight row, exactly as for an uncharged card."""
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    await _card_leg(realdb, inv_id, card_status="charged", charged="500.00")

    assert await _apply(realdb, inv_id, "paid") == report.HELD_PAID_WHILE_SCHEDULED
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.erp_report["kind"] == "paid_while_scheduled"


async def test_paid_over_a_run_card_leg_settled_in_full_moves_to_paid(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    await _card_leg(realdb, inv_id, card_status="completed", charged="500.00")

    assert await _apply(realdb, inv_id, "paid") == report.TRANSITIONED
    assert await _status(realdb, inv_id) == InvoiceStatus.paid
    assert await _exceptions(realdb, inv_id) == []


async def test_paid_over_a_run_card_leg_charged_short_is_held_settled_short(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    pay_id = await _card_leg(realdb, inv_id, card_status="completed", charged="300.00")

    assert await _apply(realdb, inv_id, "paid") == report.HELD_SETTLED_SHORT
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled
    [exc] = await _exceptions(realdb, inv_id)
    assert exc.erp_report["kind"] == "settled_short"
    assert exc.erp_report["payment_id"] == str(pay_id)
    assert exc.erp_report["shortfall"] == "200.00"


async def test_paid_in_flight_is_not_swallowed_by_an_unrelated_blocking_row(realdb):
    """An unrelated live `payment_reconciliation` does not stand in for the
    double-pay flag: once a person resolves it, the invoice is still blocked."""
    inv_id = await _seed(realdb, InvoiceStatus.payment_scheduled)
    unrelated = await _exception(realdb, inv_id, "payment_reconciliation", "bill_state")

    assert await _apply(realdb, inv_id, "paid") == report.HELD_PAID_WHILE_SCHEDULED
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(ApException).where(ApException.id == unrelated).values(status="resolved")
        )
        await s.commit()

    assert await _blocked(realdb, inv_id)
    assert await _status(realdb, inv_id) == InvoiceStatus.payment_scheduled


# ---------------------------------------------------------------------------
# a non-paid status withdraws an unrecorded ERP payment (§319)
# ---------------------------------------------------------------------------


async def test_a_non_paid_status_withdraws_an_unrecorded_erp_payment(realdb):
    inv_id = await _seed(realdb, InvoiceStatus.posted_in_erp)
    exc_id = await _exception(realdb, inv_id, "erp_reconciliation", "paid_not_recorded")

    # `posted_in_erp → posted_in_erp` is a no-op edge, but the withdrawal is a
    # change, so the outcome asks for a commit.
    assert await _apply(realdb, inv_id, "posted") == report.WITHDRAWN
    async with realdb.sessionmaker(TENANT)() as s:
        row = await s.get(ApException, exc_id)
    assert row.erp_report["kind"] == "paid_withdrawn"
    assert row.status == "open"
