"""`DELETE /api/invoices/{id}` must cascade through every NO ACTION FK.

`_delete_invoice_cascade` used to clear only six tables (exceptions,
payments, payment_schedules, workflow_instances, invoice_extraction_results,
invoice_line_items). Every other table with a `NO ACTION` FK to invoices
(virtual_cards, agent_decisions, peppol_transmissions, credit_memos,
discount_offers, supplier_chat_threads, vendor_statement_recon_lines) was
missing — deleting an invoice that had one of those rows raised an unhandled
IntegrityError (500) instead of a clean delete. Grandchildren of those
tables (card_reveal_tokens/card_rebates under virtual_cards,
supplier_chat_messages under supplier_chat_threads) needed clearing first.
corporate_card_transactions is a separate case: it's an independently
imported bank-statement feed reconciled TO a virtual card, not owned by the
invoice, so it's unlinked (FK set NULL) rather than deleted.

Found live by exploratory persona-driven testing (approver persona).
Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.credit_memo import CreditMemo
from app.models.expense import CorporateCardTransaction
from app.models.invoice import Invoice, InvoiceStatus
from app.models.supplier_chat import (
    ChatAuthorRole,
    ChatThreadStatus,
    SupplierChatMessage,
    SupplierChatThread,
)
from app.models.vendor import Vendor
from app.models.virtual_card import CardRebate, CardRevealToken, VirtualCard
from app.models.workflow import AuditLog

pytestmark = pytest.mark.asyncio

TENANT = "a"


async def _seed_invoice(mk, org_id, *, number: str) -> str:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Delete Cascade Vendor",
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


async def test_delete_invoice_with_virtual_card_succeeds(realdb):
    """The exact live repro: an approved invoice with a virtual card (plus its
    own reveal token + rebate) must delete cleanly, not 500. The card is
    cancelled: a live one refuses the delete (decisions §577, below)."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELCASC-CARD")

    async with mk() as s:
        card = VirtualCard(
            organization_id=org_id,
            invoice_id=uuid.UUID(invoice_id),
            card_provider="mock",
            provider_card_id=f"card_{uuid.uuid4().hex[:8]}",
            amount_limit=Decimal("500.00"),
            status="cancelled",
        )
        s.add(card)
        await s.flush()
        s.add(
            CardRevealToken(
                token_hash="a" * 64,
                card_id=card.id,
                organization_id=org_id,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        s.add(
            CardRebate(
                organization_id=org_id,
                virtual_card_id=card.id,
                amount=Decimal("7.50"),
                rate=Decimal("0.0150"),
            )
        )
        s.add(
            CorporateCardTransaction(
                organization_id=org_id,
                virtual_card_id=card.id,
                txn_date=date.today(),
                amount=Decimal("500.00"),
            )
        )
        await s.commit()
        card_id = card.id

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204, resp.text

    async with mk() as s:
        assert (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).first() is None
        assert (
            await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))
        ).first() is None
        assert (
            await s.execute(select(CardRevealToken).where(CardRevealToken.card_id == card_id))
        ).first() is None
        assert (
            await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id))
        ).first() is None
        # Unlinked, not deleted — the bank-statement feed row survives.
        txn = (
            await s.execute(
                select(CorporateCardTransaction).where(
                    CorporateCardTransaction.organization_id == org_id
                )
            )
        ).scalar_one()
        assert txn.virtual_card_id is None


async def test_delete_invoice_with_applied_credit_memo_succeeds(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELCASC-MEMO")

    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Delete Cascade Vendor")
        s.add(vendor)
        await s.flush()
        s.add(
            CreditMemo(
                memo_number="CM-DELCASC",
                vendor_id=vendor.id,
                invoice_id=uuid.UUID(invoice_id),
                amount=Decimal("50.00"),
                status="applied",
                organization_id=org_id,
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204, resp.text

    async with mk() as s:
        assert (
            await s.execute(select(CreditMemo).where(CreditMemo.memo_number == "CM-DELCASC"))
        ).first() is None


async def test_delete_invoice_with_supplier_chat_thread_succeeds(realdb):
    """Grandchild-of-grandchild ordering: messages must clear before their
    thread, and the thread before the invoice."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELCASC-CHAT")

    async with mk() as s:
        thread = SupplierChatThread(
            organization_id=org_id,
            invoice_id=uuid.UUID(invoice_id),
            status=ChatThreadStatus.open,
        )
        s.add(thread)
        await s.flush()
        s.add(
            SupplierChatMessage(
                thread_id=thread.id,
                author_role=ChatAuthorRole.ap_team,
                author_user_id=None,
                body="test message",
            )
        )
        await s.commit()
        thread_id = thread.id

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204, resp.text

    async with mk() as s:
        assert (
            await s.execute(select(SupplierChatThread).where(SupplierChatThread.id == thread_id))
        ).first() is None
        assert (
            await s.execute(
                select(SupplierChatMessage).where(SupplierChatMessage.thread_id == thread_id)
            )
        ).first() is None


# ---------------------------------------------------------------------------
# A hard delete is a tenant mutation, and it was the one leaving no record.
#
# `_delete_invoice_cascade` removes the invoice plus its exceptions, payments,
# payment schedules, workflow instances/steps, extraction results, line items,
# agent decisions, PEPPOL transmissions, credit memos, discount offers, chat
# threads and virtual cards. It deliberately does NOT touch `audit_log`, which
# is what lets a terminal `invoice.deleted` row survive the delete and keeps the
# invoice's whole prior trail readable afterwards.
#
# The audit sweep in `test_audit_append_only.py` did not catch this: it asks
# whether the string `dispatch_audit` appears anywhere in the MODULE, and
# `api/invoices.py` has 21 tenant-mutating routes, so one auditing handler
# exempted the other twenty.
# ---------------------------------------------------------------------------


async def _deletion_rows(mk, invoice_id: str) -> list[AuditLog]:
    async with mk() as s:
        rows = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == uuid.UUID(invoice_id),
                    AuditLog.action == "invoice.deleted",
                )
            )
        ).scalars()
        return list(rows)


async def test_deleting_an_invoice_writes_a_surviving_audit_row(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    invoice_id = await _seed_invoice(mk, org_id, number="DELAUDIT-1")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{invoice_id}")
    assert resp.status_code == 204

    # The invoice is gone...
    async with mk() as s:
        assert (
            await s.execute(select(Invoice).where(Invoice.id == uuid.UUID(invoice_id)))
        ).scalar_one_or_none() is None

    # ...and the record of who removed it is not.
    rows = await _deletion_rows(mk, invoice_id)
    assert len(rows) == 1, "a hard delete must leave exactly one terminal audit row"
    row = rows[0]
    assert row.actor_id is not None
    assert row.details["invoice_number"] == "DELAUDIT-1"
    assert row.details["bulk"] is False
    # PII-free: the business reference and the status, nothing about the payee.
    assert "vendor_name" not in row.details
    assert "remit_to_address" not in row.details


async def test_bulk_delete_audits_each_invoice_it_actually_deletes(realdb):
    """And only those — a skipped (immutable-status) invoice must not be
    recorded as deleted, or the trail asserts a deletion that never happened."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    deletable = await _seed_invoice(mk, org_id, number="DELAUDIT-BULK-OK")

    async with mk() as s:
        blocked = Invoice(
            organization_id=org_id,
            invoice_number="DELAUDIT-BULK-SKIP",
            vendor_name="Delete Cascade Vendor",
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.paid,  # in IMMUTABLE_STATUSES
        )
        s.add(blocked)
        await s.commit()
        await s.refresh(blocked)
        blocked_id = str(blocked.id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [deletable, blocked_id]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 1
    assert body["skipped"] == [blocked_id]

    assert len(await _deletion_rows(mk, deletable)) == 1
    assert await _deletion_rows(mk, blocked_id) == []


# ---------------------------------------------------------------------------
# An approved invoice is not immutable, but it can already be in a payment run
# — exported to the bank, even. Deleting it cascaded the live payment away
# while the bank still paid it, so the ledger stopped matching the money that
# moved. Both delete doors now refuse it; the way out is cancel / void.
# ---------------------------------------------------------------------------


async def _with_pending_payment(mk, invoice_id: str) -> None:
    from app.models.payment import Payment

    async with mk() as s:
        s.add(
            Payment(
                invoice_id=uuid.UUID(invoice_id),
                amount=Decimal("500.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()


async def _still_there(mk, invoice_id: str) -> bool:
    async with mk() as s:
        row = await s.execute(select(Invoice.id).where(Invoice.id == uuid.UUID(invoice_id)))
        return row.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_delete_refuses_an_invoice_with_a_live_payment(realdb):
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, realdb.info(TENANT).org_id, number="DEL-LIVE-PAY-1")
    await _with_pending_payment(mk, inv_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "invoice_delete_claimed"
    assert detail["params"] == {"claim": "payment"}
    assert await _still_there(mk, inv_id)


@pytest.mark.asyncio
async def test_bulk_delete_skips_an_invoice_with_a_live_payment(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    paying = await _seed_invoice(mk, org_id, number="DEL-LIVE-PAY-2")
    free = await _seed_invoice(mk, org_id, number="DEL-LIVE-PAY-3")
    await _with_pending_payment(mk, paying)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [paying, free]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "skipped": [paying]}
    assert await _still_there(mk, paying)
    assert not await _still_there(mk, free)


# ---------------------------------------------------------------------------
# A live virtual card is a claim too (decisions §577): bearer-spendable for the
# face amount, or already charged. The cascade deleted it with the invoice, so
# the ledger lost a card that could still move (or had moved) money. Both doors
# now refuse it, by the card rail's own predicate (`status <> 'cancelled'`);
# cancelling the card is the way out.
# ---------------------------------------------------------------------------


async def _with_card(mk, org_id, invoice_id: str, status: str) -> uuid.UUID:
    async with mk() as s:
        card = VirtualCard(
            organization_id=org_id,
            invoice_id=uuid.UUID(invoice_id),
            card_provider="mock",
            provider_card_id=f"card_{uuid.uuid4().hex[:8]}",
            amount_limit=Decimal("500.00"),
            status=status,
        )
        s.add(card)
        await s.commit()
        return card.id


@pytest.mark.asyncio
@pytest.mark.parametrize("card_status", ["active", "charged"])
async def test_delete_refuses_an_invoice_a_live_card_holds(realdb, card_status):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number=f"DEL-LIVE-CARD-{card_status}")
    card_id = await _with_card(mk, org_id, inv_id, card_status)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "invoice_delete_claimed"
    assert detail["params"] == {"claim": "virtual_card"}
    assert await _still_there(mk, inv_id)
    async with mk() as s:
        assert (await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))).first()
    assert await _deletion_rows(mk, inv_id) == []


@pytest.mark.asyncio
async def test_delete_goes_through_once_the_card_is_cancelled(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-LIVE-CARD-CANCEL")
    card_id = await _with_card(mk, org_id, inv_id, "active")

    async with realdb.client(key=TENANT, role="admin") as c:
        assert (await c.delete(f"/api/invoices/{inv_id}")).status_code == 409
        async with mk() as s:
            card = (
                await s.execute(select(VirtualCard).where(VirtualCard.id == card_id))
            ).scalar_one()
            card.status = "cancelled"
            await s.commit()
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 204, resp.text
    assert not await _still_there(mk, inv_id)


@pytest.mark.asyncio
async def test_bulk_delete_skips_an_invoice_a_live_card_holds(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    carded = await _seed_invoice(mk, org_id, number="DEL-LIVE-CARD-B1")
    free = await _seed_invoice(mk, org_id, number="DEL-LIVE-CARD-B2")
    await _with_card(mk, org_id, carded, "active")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [carded, free]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "skipped": [carded]}
    assert await _still_there(mk, carded)
    assert not await _still_there(mk, free)


# ---------------------------------------------------------------------------
# A voided payment returns the invoice to `approved`, so it can be deleted,
# while the ERP side of the void (or a discount credit) is still unfinished in
# `WorkflowInstance.state_data`. The cascade deleted that instance and the
# BillPayment / credit stood in the ERP untracked; both doors now refuse while
# any of the three pending maps holds an entry (decisions §580).
# ---------------------------------------------------------------------------


async def _with_state(mk, org_id, invoice_id: str, state_data: dict) -> None:
    from app.models.workflow import WorkflowDefinition, WorkflowInstance

    async with mk() as s:
        defn = WorkflowDefinition(
            organization_id=org_id, name=f"del-{uuid.uuid4().hex[:6]}", steps_config={"steps": []}
        )
        s.add(defn)
        await s.flush()
        s.add(
            WorkflowInstance(
                definition_id=defn.id,
                invoice_id=uuid.UUID(invoice_id),
                state="active",
                state_data=state_data,
            )
        )
        await s.commit()


_PENDING_STATES = {
    "void": {"erp_payment_voids_pending": [str(uuid.UUID(int=1))]},
    "void_credit": {"erp_discount_credit_voids_pending": {str(uuid.UUID(int=2)): "VC-1"}},
    "credit": {"erp_discount_credits_pending": {str(uuid.UUID(int=3)): {"credit_id": None}}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", sorted(_PENDING_STATES))
async def test_delete_refuses_while_an_erp_reversal_is_pending(realdb, pending):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number=f"DEL-ERP-PEND-{pending}")
    await _with_state(mk, org_id, inv_id, _PENDING_STATES[pending])

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "invoice_delete_erp_reversal_pending"
    assert detail["params"] == {}
    assert await _still_there(mk, inv_id)
    assert await _deletion_rows(mk, inv_id) == []


@pytest.mark.asyncio
async def test_delete_goes_through_when_the_pending_maps_are_empty(realdb):
    """Other state_data, or a key left empty, is not an unfinished reversal."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-ERP-PEND-NONE")
    await _with_state(mk, org_id, inv_id, {"erp_payment_voids_pending": [], "approval_level": 1})

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 204, resp.text
    assert not await _still_there(mk, inv_id)


@pytest.mark.asyncio
async def test_bulk_delete_skips_an_invoice_with_a_pending_erp_reversal(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    pending = await _seed_invoice(mk, org_id, number="DEL-ERP-PEND-B1")
    free = await _seed_invoice(mk, org_id, number="DEL-ERP-PEND-B2")
    await _with_state(mk, org_id, pending, _PENDING_STATES["void"])

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [pending, free]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "skipped": [pending]}
    assert await _still_there(mk, pending)
    assert not await _still_there(mk, free)
    assert await _deletion_rows(mk, pending) == []


# ---------------------------------------------------------------------------
# A PEPPOL transmission not `failed` is the local record of a legally
# significant e-invoice the receiver may hold; the cascade erased it. Deleting
# is refused, as the recall is (§573): a credit note corrects it (§580). A
# `failed` transmission never left, and the cascade still clears it.
# ---------------------------------------------------------------------------


async def _with_transmission(
    mk, org_id, invoice_id: str, status: str, *, direction: str = "outbound"
) -> uuid.UUID:
    from app.models.peppol_transmission import PeppolTransmission

    async with mk() as s:
        row = PeppolTransmission(
            invoice_id=uuid.UUID(invoice_id),
            direction=direction,
            participant_scheme="9930",
            participant_value="SUPPLIER123",
            doc_type_id="doc",
            process_id="proc",
            business_message_id=uuid.uuid4().hex,
            status=status,
            provider="mock",
            organization_id=org_id,
        )
        s.add(row)
        await s.commit()
        return row.id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sending", "sent", "delivered"])
async def test_delete_refuses_an_invoice_sent_over_peppol(realdb, status):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number=f"DEL-PEPPOL-{status}")
    await _with_transmission(mk, org_id, inv_id, status)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "invoice_delete_peppol_transmitted"
    assert detail["params"] == {}
    assert await _still_there(mk, inv_id)


@pytest.mark.asyncio
async def test_delete_clears_a_failed_peppol_transmission(realdb):
    from app.models.peppol_transmission import PeppolTransmission

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-PEPPOL-FAILED")
    await _with_transmission(mk, org_id, inv_id, "failed")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 204, resp.text
    async with mk() as s:
        left = await s.execute(
            select(PeppolTransmission.id).where(PeppolTransmission.invoice_id == uuid.UUID(inv_id))
        )
        assert left.first() is None


@pytest.mark.asyncio
async def test_bulk_delete_skips_an_invoice_sent_over_peppol(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    sent = await _seed_invoice(mk, org_id, number="DEL-PEPPOL-B1")
    free = await _seed_invoice(mk, org_id, number="DEL-PEPPOL-B2")
    await _with_transmission(mk, org_id, sent, "sent")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [sent, free]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "skipped": [sent]}
    assert await _still_there(mk, sent)
    assert not await _still_there(mk, free)


@pytest.mark.asyncio
async def test_a_live_claim_is_named_before_a_peppol_transmission(realdb):
    """A claim can be cleared (cancel the card); a transmission cannot, so the
    claim is named first."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-PEPPOL-CLAIM")
    await _with_card(mk, org_id, inv_id, "active")
    await _with_transmission(mk, org_id, inv_id, "sent")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "invoice_delete_claimed"


# ---------------------------------------------------------------------------
# An INBOUND transmission is the record of an e-invoice a supplier delivered to
# us. Deleting its invoice (a spam or duplicate document) is allowed, but the
# row is detached and stamped, never deleted (§586).
# ---------------------------------------------------------------------------


async def _transmission(mk, row_id: uuid.UUID):
    from app.models.peppol_transmission import PeppolTransmission

    async with mk() as s:
        return (
            await s.execute(select(PeppolTransmission).where(PeppolTransmission.id == row_id))
        ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_delete_keeps_a_received_transmission_detached_and_marked(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-PEPPOL-IN")
    inbound = await _with_transmission(mk, org_id, inv_id, "delivered", direction="inbound")
    # A failed outbound attempt on the same invoice never left: still cleared.
    failed = await _with_transmission(mk, org_id, inv_id, "failed")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv_id}")
    assert resp.status_code == 204, resp.text
    assert not await _still_there(mk, inv_id)

    row = await _transmission(mk, inbound)
    assert row is not None, "the record of a received e-invoice must survive the delete"
    assert row.invoice_id is None
    assert row.deleted_invoice_id == uuid.UUID(inv_id)
    assert row.invoice_deleted_at is not None
    # The stamp names the same person the `invoice.deleted` audit row does.
    (audit,) = await _deletion_rows(mk, inv_id)
    assert row.invoice_deleted_by_id == audit.actor_id is not None
    # Nothing else about the received document changed.
    assert row.status == "delivered" and row.participant_value == "SUPPLIER123"
    assert await _transmission(mk, failed) is None


@pytest.mark.asyncio
async def test_bulk_delete_keeps_a_received_transmission_detached(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-PEPPOL-IN-BULK")
    inbound = await _with_transmission(mk, org_id, inv_id, "delivered", direction="inbound")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [inv_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1, "skipped": []}

    row = await _transmission(mk, inbound)
    assert row is not None and row.invoice_id is None
    assert row.deleted_invoice_id == uuid.UUID(inv_id)
    assert row.invoice_deleted_by_id is not None and row.invoice_deleted_at is not None


@pytest.mark.asyncio
async def test_only_an_inbound_stamped_row_may_lose_its_invoice(realdb):
    """`ck_peppol_detached_inbound` holds the rule at the data layer."""
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from app.models.peppol_transmission import PeppolTransmission

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-PEPPOL-CK")
    outbound = await _with_transmission(mk, org_id, inv_id, "failed")
    inbound = await _with_transmission(mk, org_id, inv_id, "delivered", direction="inbound")

    for row_id in (outbound, inbound):  # outbound never; inbound not unstamped
        async with mk() as s:
            with pytest.raises(IntegrityError, match="ck_peppol_detached_inbound"):
                await s.execute(
                    update(PeppolTransmission)
                    .where(PeppolTransmission.id == row_id)
                    .values(invoice_id=None)
                )


# ---------------------------------------------------------------------------
# The pending maps are read under the instance's row lock (decisions §581): a
# write committing while the delete runs — a discount credit's reservation, a
# void a write-back re-arms — is waited for and seen, not read from the
# snapshot before it and then cascaded away.
# ---------------------------------------------------------------------------


async def _someone_waits_on_a_lock(mk, *, timeout_s: float = 10.0) -> bool:
    import asyncio

    from sqlalchemy import text

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_delete_waits_for_a_pending_write_on_the_instance_and_refuses(realdb):
    import asyncio

    from app.models.workflow import WorkflowInstance

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = await _seed_invoice(mk, org_id, number="DEL-ERP-PEND-RACE")
    await _with_state(mk, org_id, inv_id, {"approval_level": 1})

    async with mk() as writer:
        instance = (
            await writer.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.invoice_id == uuid.UUID(inv_id))
                .with_for_update()
            )
        ).scalar_one()
        instance.state_data = {
            **instance.state_data,
            "erp_discount_credits_pending": {str(uuid.UUID(int=4)): {"credit_id": None}},
        }
        await writer.flush()
        async with realdb.client(key=TENANT, role="admin") as c:
            delete = asyncio.create_task(c.delete(f"/api/invoices/{inv_id}"))
            try:
                assert await _someone_waits_on_a_lock(mk), "the delete did not wait"
                assert not delete.done()
                await writer.commit()
                resp = await asyncio.wait_for(delete, timeout=30)
            finally:
                if not delete.done():
                    delete.cancel()
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "invoice_delete_erp_reversal_pending"
    assert await _still_there(mk, inv_id)
