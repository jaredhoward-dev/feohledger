"""Voiding a flagged bill in the ERP from its exception (decisions §539).

A `duplicate` / `fraud_flag` can open after the bill is already in the ERP,
where the ERP's own Bill Pay can pay it past our gate. Pinned here:

* the state machine: `sent_to_erp` / `posted_in_erp → approved` exists only as
  this void; any other action is refused as an invalid edge (DB-free);
* `POST /api/exceptions/{id}/void-erp-bill` (real Postgres, real HTTP): gated on
  `payment.void` (admin yes, ap_manager no), refuses a row that is not a flag,
  an invoice with no bill, a live payment, a bill the ERP reads paid, an
  adapter that cannot or did not void; voids through the adapter, clears the
  document, walks to `approved`, writes both audit rows, leaves the flag open,
  and a replay answers 200 without calling the ERP again;
* the queue's verdict and the raise-time notice.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.notification import Notification
from app.models.workflow import AuditLog, WorkflowInstance
from app.services.erp_adapters.base import ErpInvoiceStatus

TENANT = "a"


# ---------------------------------------------------------------------------
# DB-free: the edge and the verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [InvoiceStatus.sent_to_erp, InvoiceStatus.posted_in_erp])
def test_the_void_edge_is_walked_by_the_void_alone(source):
    from app.services.workflow_engine import (
        ERP_VOID_ACTION,
        ERP_VOID_EDGES,
        VALID_TRANSITIONS,
        _refuse_outside_the_recall,
    )

    assert (source, InvoiceStatus.approved) in ERP_VOID_EDGES
    assert InvoiceStatus.approved in VALID_TRANSITIONS[source]
    inv = SimpleNamespace(status=source, approved_at=None)
    _refuse_outside_the_recall(inv, InvoiceStatus.approved, ERP_VOID_ACTION)
    with pytest.raises(HTTPException) as refused:
        _refuse_outside_the_recall(inv, InvoiceStatus.approved, "invoice.bulk_status_changed")
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "invoice_transition_invalid"


def test_the_payable_sets_are_unchanged_by_the_new_edges():
    from app.services.payment_gates import SCHEDULABLE_INVOICE_STATUSES

    assert "sent_to_erp" not in SCHEDULABLE_INVOICE_STATUSES


def _user(perms):
    return SimpleNamespace(id=uuid.uuid4(), permissions=frozenset(perms), roles=[])


def _exc(exception_type="duplicate", status="open"):
    return SimpleNamespace(
        id=uuid.uuid4(), exception_type=exception_type, status=status, invoice_id=uuid.uuid4()
    )


def _inv(status=InvoiceStatus.posted_in_erp, doc="BILL-1"):
    return SimpleNamespace(status=status, erp_document_id=doc)


def test_verdict_order_permission_then_row_then_bill():
    from app.services.erp_bill_void import void_refusal

    with patch("app.api.deps.holds_any_permission", return_value=False):
        assert void_refusal(_exc(), _inv(), _user(()))["code"] == "role_not_permitted"
    with patch("app.api.deps.holds_any_permission", return_value=True):
        assert void_refusal(_exc("po_mismatch"), _inv(), _user(()))["code"] == (
            "erp_void_not_flagged"
        )
        assert void_refusal(_exc(status="resolved"), _inv(), _user(()))["code"] == (
            "erp_void_not_flagged"
        )
        assert void_refusal(_exc(), _inv(doc=None), _user(()))["code"] == "erp_void_no_bill"
        assert void_refusal(_exc(), _inv(InvoiceStatus.approved), _user(()))["code"] == (
            "erp_void_no_bill"
        )
        assert void_refusal(_exc("fraud_flag", "escalated"), _inv(), _user(())) is None


# ---------------------------------------------------------------------------
# real Postgres, real HTTP
# ---------------------------------------------------------------------------


class _Adapter:
    def __init__(self, *, read=ErpInvoiceStatus.open, voids=True, unsupported=False):
        self.read = read
        self.voids = voids
        self.unsupported = unsupported
        self.voided: list[str] = []

    async def get_invoice_status(self, doc):
        return self.read

    async def void_invoice(self, doc):
        if self.unsupported:
            raise NotImplementedError
        self.voided.append(doc)
        return self.voids


def _adapter(adapter):
    return patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter)


async def _seed(mk, org_id, *, number, status=InvoiceStatus.posted_in_erp, doc="BILL-9"):
    from app.services.workflow_engine import get_or_create_workflow_definition

    inv_id = uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Northwind Supplies",
            amount=Decimal("1234.56"),
            currency="USD",
            status=status,
            approved_by="Approver",
            erp_document_id=doc,
        )
        s.add(inv)
        await s.flush()
        defn = await get_or_create_workflow_definition(s, org_id)
        s.add(
            WorkflowInstance(
                correlation_id=inv.correlation_id,
                definition_id=defn.id,
                invoice_id=inv_id,
                current_step=3,
                state="active",
                state_data={"erp_reference": doc} if doc else {},
                steps_config_snapshot={"steps": []},
            )
        )
        await s.commit()
    return inv_id


async def _flag(mk, org_id, inv_id, exception_type="duplicate"):
    from app.services.exception_service import create_exception

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        exc = await create_exception(
            s,
            exception_type=exception_type,
            severity="error",
            description="detector output",
            organization_id=org_id,
            invoice=inv,
            raised_by_user_id=None,
        )
        await s.commit()
        return exc.id


async def _audit(mk, entity_id, action):
    async with mk() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == entity_id, AuditLog.action == action)
        )
        return list(rows.scalars())


async def test_admin_voids_the_bill_once_and_a_replay_calls_nothing(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VOID-001")
    exc_id = await _flag(mk, info.org_id, inv_id)
    adapter = _Adapter()

    async with realdb.client(key=TENANT, role="admin") as c:
        detail = await c.get(f"/api/exceptions/{exc_id}")
        assert detail.json()["void_erp_bill_refusal"] is None
        with _adapter(adapter):
            first = await c.post(
                f"/api/exceptions/{exc_id}/void-erp-bill", json={"reason": "Confirmed duplicate"}
            )
            again = await c.post(
                f"/api/exceptions/{exc_id}/void-erp-bill", json={"reason": "Confirmed duplicate"}
            )

    assert first.status_code == 201, first.text
    assert first.json()["invoice_status"] == "approved"
    assert first.json()["erp_document_id"] == "BILL-9"
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert adapter.voided == ["BILL-9"]

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.approved
        assert inv.erp_document_id is None
        inst = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
        assert "erp_reference" not in inst.state_data
        assert inst.state_data["erp_bill_voided"]["document_id"] == "BILL-9"
        # The flag stays for a person to decide.
        assert (await s.get(APException, exc_id)).status == "open"

    rows = await _audit(mk, inv_id, "invoice.erp_bill_voided")
    assert len(rows) == 1
    assert rows[0].details["old_status"] == "posted_in_erp"
    assert rows[0].details["new_status"] == "approved"
    assert len(await _audit(mk, exc_id, "exception.erp_bill_voided")) == 1


async def test_ap_manager_without_payment_void_is_refused(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VOID-002")
    exc_id = await _flag(mk, info.org_id, inv_id)
    adapter = _Adapter()
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        detail = await c.get(f"/api/exceptions/{exc_id}")
        assert detail.json()["void_erp_bill_refusal"]["code"] == "role_not_permitted"
        with _adapter(adapter):
            res = await c.post(f"/api/exceptions/{exc_id}/void-erp-bill", json={"reason": "x"})
    assert res.status_code == 403
    assert adapter.voided == []


@pytest.mark.parametrize(
    ("adapter", "status_code", "code"),
    [
        (_Adapter(read=ErpInvoiceStatus.paid), 409, "erp_void_bill_paid"),
        (_Adapter(read=ErpInvoiceStatus.partially_paid), 409, "erp_void_bill_paid"),
        (_Adapter(voids=False), 409, "erp_void_rejected"),
        (_Adapter(unsupported=True), 409, "erp_void_unsupported"),
    ],
)
async def test_the_erp_refusals_change_nothing(realdb, adapter, status_code, code):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"VOID-R-{uuid.uuid4().hex[:6]}")
    exc_id = await _flag(mk, info.org_id, inv_id, "fraud_flag")
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(f"/api/exceptions/{exc_id}/void-erp-bill", json={"reason": "x"})
    assert res.status_code == status_code, res.text
    assert res.json()["detail"]["code"] == code
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.posted_in_erp
        assert inv.erp_document_id == "BILL-9"
    assert await _audit(mk, inv_id, "invoice.erp_bill_voided") == []


async def test_no_bill_and_not_a_flag_are_refused_before_the_erp(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    no_bill = await _seed(
        mk, info.org_id, number="VOID-003", status=InvoiceStatus.approved, doc=None
    )
    exc_no_bill = await _flag(mk, info.org_id, no_bill)
    posted = await _seed(mk, info.org_id, number="VOID-004")
    exc_other = await _flag(mk, info.org_id, posted, "line_total_mismatch")
    adapter = _Adapter()
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            r1 = await c.post(f"/api/exceptions/{exc_no_bill}/void-erp-bill", json={"reason": "x"})
            r2 = await c.post(f"/api/exceptions/{exc_other}/void-erp-bill", json={"reason": "x"})
            r3 = await c.post(f"/api/exceptions/{uuid.uuid4()}/void-erp-bill", json={"reason": "x"})
    assert (r1.status_code, r1.json()["detail"]["code"]) == (409, "erp_void_no_bill")
    assert (r2.status_code, r2.json()["detail"]["code"]) == (409, "erp_void_not_flagged")
    assert r3.status_code == 404
    assert adapter.voided == []


async def test_a_live_payment_refuses_the_void(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="VOID-005")
    exc_id = await _flag(mk, info.org_id, inv_id)
    adapter = _Adapter()

    async def _claimed(db, ids):
        return dict.fromkeys(ids, "payment")

    with patch("app.services.erp_export_gate.live_claims", _claimed):
        with _adapter(adapter):
            async with realdb.client(key=TENANT, role="admin") as c:
                res = await c.post(f"/api/exceptions/{exc_id}/void-erp-bill", json={"reason": "x"})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "erp_void_claimed"
    assert res.json()["detail"]["params"] == {"claim": "payment"}
    assert adapter.voided == []


async def test_a_flag_on_a_posted_bill_notifies_and_one_on_an_unposted_does_not(
    realdb, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "notifications_enabled", True)
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    posted = await _seed(mk, info.org_id, number="VOID-006")
    unposted = await _seed(
        mk, info.org_id, number="VOID-007", status=InvoiceStatus.approved, doc=None
    )
    posted_exc = await _flag(mk, info.org_id, posted)
    unposted_exc = await _flag(mk, info.org_id, unposted)
    async with mk() as s:
        rows = list(
            (
                await s.execute(
                    select(Notification).where(Notification.event_type == "erp_bill_flagged")
                )
            ).scalars()
        )
    ids = {r.entity_id for r in rows}
    assert posted_exc in ids
    assert unposted_exc not in ids
    assert all(r.entity_type == "exception" for r in rows)
    assert all("Northwind" not in (r.title + (r.body or "")) for r in rows)
