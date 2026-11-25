"""The bill a failed push may have left in the ERP (decisions §556).

A push that timed out or got a 5xx may have created the bill; the gate's
refusal of the retry says `possibly_posted: true` (§286). Pinned here:

* `GET /api/invoices/{id}/erp-orphan-bill` runs the adapter's read-only
  lookup (`ErpAdapter.find_posted_bill`) and names the bill — found / not
  found / an adapter with no lookup / a lookup that cannot finish — gated on
  `payment.void`, audited append-only, and it writes nothing to the ERP;
* `POST /api/invoices/{id}/void-erp-orphan-bill` voids that bill through the
  flag void's own three steps (`services/erp_bill_void`): the marker, the
  positive read plus the lookup run again, the finish; the invoice stays
  `failed`; a replay calls nothing; a bill the lookup no longer names is
  refused; the retry door refuses while the void's marker is live;
* the mock and QuickBooks adapters' lookups.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog, WorkflowInstance
from app.services.erp_adapters.base import (
    BILL_VOID_ANY,
    ErpBillReport,
    ErpBillState,
    ErpInvoiceStatus,
    FoundBill,
)

TENANT = "a"


# ---------------------------------------------------------------------------
# DB-free
# ---------------------------------------------------------------------------


def _user():
    return SimpleNamespace(id=uuid.uuid4(), permissions=frozenset(), roles=[])


def test_the_candidate_is_a_failed_push_that_may_have_posted():
    from app.services.erp_bill_void import orphan_candidate

    unknown = SimpleNamespace(state_data={"erp_outcome_unknown": True})
    clean = SimpleNamespace(state_data={"erp_outcome_unknown": False})
    failed = SimpleNamespace(status=InvoiceStatus.failed, erp_document_id=None)
    assert orphan_candidate(failed, unknown) is True
    assert orphan_candidate(failed, clean) is False
    assert orphan_candidate(failed, None) is False
    assert (
        orphan_candidate(
            SimpleNamespace(status=InvoiceStatus.approved, erp_document_id=None), unknown
        )
        is False
    )
    assert (
        orphan_candidate(SimpleNamespace(status=InvoiceStatus.failed, erp_document_id="B"), unknown)
        is False
    )


def test_the_lookup_verdict_checks_the_permission_first():
    from app.services.erp_bill_void import orphan_lookup_refusal

    inv = SimpleNamespace(status=InvoiceStatus.failed, erp_document_id=None)
    inst = SimpleNamespace(state_data={"erp_outcome_unknown": True})
    with patch("app.api.deps.holds_any_permission", return_value=False):
        assert orphan_lookup_refusal(inv, inst, _user())["code"] == "role_not_permitted"
    with patch("app.api.deps.holds_any_permission", return_value=True):
        assert orphan_lookup_refusal(inv, inst, _user()) is None
        inst.state_data = {}
        assert orphan_lookup_refusal(inv, inst, _user())["code"] == "erp_lookup_not_failed"


def test_only_adapters_with_a_lookup_offer_it():
    from app.services.erp_adapters.base import ErpAdapter
    from app.services.erp_bill_void import lookup_supported

    assert ErpAdapter.supports_bill_lookup is False
    assert lookup_supported(None) is True  # the local mock
    assert (
        lookup_supported({"erp": {"type": "quickbooks_online", "integration_method": "direct"}})
        is True
    )
    assert lookup_supported({"erp": {"type": "xero", "integration_method": "direct"}}) is False


async def test_the_mock_lookup_finds_what_it_posted_until_voided():
    from app.services.erp_adapters.base import InvoicePayload
    from app.services.erp_adapters.mock_adapter import MockAdapter

    adapter = MockAdapter({})
    payload = InvoicePayload(
        correlation_id=str(uuid.uuid4()),
        invoice_number="MOCK-1",
        vendor_name="V",
        amount=Decimal("10.00"),
        currency="USD",
    )
    assert await adapter.find_posted_bill(payload) is None
    posted = await adapter.post_invoice(payload)
    found = await adapter.find_posted_bill(payload)
    assert found.document_id == posted.erp_document_id
    assert found.amount == Decimal("10.00")
    assert await adapter.void_invoice(found.document_id) is True
    assert await adapter.find_posted_bill(payload) is None


async def test_the_quickbooks_lookup_is_its_push_lookup_and_reads_only(monkeypatch):
    from app.services.erp_adapters.base import InvoicePayload
    from app.services.erp_adapters.quickbooks_online import (
        QuickBooksOnlineAdapter,
        _private_note,
    )

    cid = str(uuid.uuid4())
    statements: list[str] = []

    async def _query(self, statement):
        statements.append(statement)
        return {
            "Bill": [
                {
                    "Id": "77",
                    "DocNumber": "QB-1",
                    "VendorRef": {"value": "V1"},
                    "PrivateNote": _private_note(cid),
                    "TotalAmt": "250.10",
                    "Balance": "250.10",
                    "CurrencyRef": {"value": "USD"},
                }
            ]
        }

    monkeypatch.setattr(QuickBooksOnlineAdapter, "_query", _query)
    adapter = QuickBooksOnlineAdapter.__new__(QuickBooksOnlineAdapter)
    payload = InvoicePayload(
        correlation_id=cid,
        invoice_number="QB-1",
        vendor_name="V",
        amount=Decimal("250.10"),
        currency="USD",
        vendor_erp_id="V1",
    )
    found = await adapter.find_posted_bill(payload)
    assert found.document_id == "77"
    assert found.amount == Decimal("250.10")
    assert found.currency == "USD"
    assert found.status == ErpInvoiceStatus.open
    assert all(s.startswith("select ") for s in statements)
    # Another push's marker is not ours.
    payload.correlation_id = str(uuid.uuid4())
    assert await adapter.find_posted_bill(payload) is None
    # Unlinked vendor: cannot match, so it raises rather than answer "none".
    payload.vendor_erp_id = None
    with pytest.raises(ValueError):
        await adapter.find_posted_bill(payload)


# ---------------------------------------------------------------------------
# Real Postgres, real HTTP
# ---------------------------------------------------------------------------


class _Adapter:
    bill_void_scope = BILL_VOID_ANY
    supports_bill_lookup = True

    def __init__(self, *, found="ORPHAN-1", lookup_raises=None, read=ErpInvoiceStatus.open):
        self.found = found
        self.lookup_raises = lookup_raises
        self.read = read
        self.lookups = 0
        self.voided: list[str] = []

    async def find_posted_bill(self, payload):
        self.lookups += 1
        if self.lookup_raises is not None:
            raise self.lookup_raises
        if self.found is None:
            return None
        return FoundBill(
            document_id=self.found,
            document_number="INV-ORPH",
            amount=Decimal("1234.56"),
            currency="USD",
            status=self.read,
        )

    async def get_invoice_status(self, doc):
        return self.read

    async def bill_report(self, doc):
        return ErpBillReport(bill_id=doc, state=ErpBillState.unknown)

    async def void_invoice(self, doc):
        self.voided.append(doc)
        self.found = None
        return True


class _NoLookup(_Adapter):
    supports_bill_lookup = False


def _adapter(adapter):
    return patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter)


async def _seed(mk, org_id, *, number, status=InvoiceStatus.failed, unknown=True):
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
                state_data={"erp_outcome_unknown": unknown, "erp_retries": 3},
                steps_config_snapshot={"steps": []},
            )
        )
        await s.commit()
    return inv_id


async def _audit(mk, entity_id, action):
    async with mk() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == entity_id, AuditLog.action == action)
        )
        return list(rows.scalars())


async def test_the_lookup_names_the_bill_and_writes_nothing_to_the_erp(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-001")
    adapter = _Adapter()
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.get(f"/api/invoices/{inv_id}/erp-orphan-bill")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["found"] is True
    assert body["bill"] == {
        "erp_document_id": "ORPHAN-1",
        "erp_document_number": "INV-ORPH",
        "amount": "1234.56",
        "currency": "USD",
        "erp_status": "open",
    }
    assert adapter.voided == []
    rows = await _audit(mk, inv_id, "invoice.erp_orphan_bill_looked_up")
    assert len(rows) == 1 and rows[0].details["erp_document_id"] == "ORPHAN-1"
    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.failed


async def test_the_lookup_says_when_the_erp_holds_none(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-002")
    with _adapter(_Adapter(found=None)):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.get(f"/api/invoices/{inv_id}/erp-orphan-bill")
    assert res.status_code == 200, res.text
    assert res.json() == {"invoice_id": str(inv_id), "found": False, "bill": None}
    rows = await _audit(mk, inv_id, "invoice.erp_orphan_bill_looked_up")
    assert rows[0].details["found"] is False


@pytest.mark.parametrize(
    "adapter, status_code, code",
    [
        (_NoLookup(), 409, "erp_lookup_unsupported"),
        (_Adapter(lookup_raises=RuntimeError("timeout")), 502, "erp_lookup_failed"),
    ],
)
async def test_a_lookup_that_cannot_answer_is_coded(realdb, adapter, status_code, code):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"ORPH-{code[-6:]}")
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.get(f"/api/invoices/{inv_id}/erp-orphan-bill")
    assert res.status_code == status_code, res.text
    assert res.json()["detail"]["code"] == code
    assert await _audit(mk, inv_id, "invoice.erp_orphan_bill_looked_up") == []


async def test_lookup_and_void_need_payment_void_and_a_failed_push(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-003")
    clean_id = await _seed(mk, info.org_id, number="ORPH-004", unknown=False)
    adapter = _Adapter()
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            assert (await c.get(f"/api/invoices/{inv_id}/erp-orphan-bill")).status_code == 403
            res = await c.post(
                f"/api/invoices/{inv_id}/void-erp-orphan-bill",
                json={"erp_document_id": "ORPHAN-1", "reason": "x"},
            )
            assert res.status_code == 403
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.get(f"/api/invoices/{clean_id}/erp-orphan-bill")
            assert res.status_code == 409
            assert res.json()["detail"]["code"] == "erp_lookup_not_failed"
            res = await c.post(
                f"/api/invoices/{inv_id}/void-erp-orphan-bill",
                json={"erp_document_id": "ORPHAN-1", "reason": "  "},
            )
            assert res.status_code == 422
    assert adapter.lookups == 0
    assert adapter.voided == []


async def test_the_void_runs_the_shared_flow_and_a_replay_calls_nothing(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-005")
    adapter = _Adapter()
    body = {"erp_document_id": "ORPHAN-1", "reason": "Push timed out; bill is a duplicate"}
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            first = await c.post(f"/api/invoices/{inv_id}/void-erp-orphan-bill", json=body)
            again = await c.post(f"/api/invoices/{inv_id}/void-erp-orphan-bill", json=body)
    assert first.status_code == 201, first.text
    assert first.json()["invoice_status"] == "failed"
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert adapter.voided == ["ORPHAN-1"]
    # The lookup ran again inside the void, before it voided.
    assert adapter.lookups == 1

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.status == InvoiceStatus.failed
        assert inv.erp_void_pending is None
        inst = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
        assert inst.state_data["erp_orphan_bills_voided"]["ORPHAN-1"]["already_gone"] is False
        # One void proves no absence of another: the possibly-posted record stays.
        assert inst.state_data["erp_outcome_unknown"] is True
    rows = await _audit(mk, inv_id, "invoice.erp_orphan_bill_voided")
    assert len(rows) == 1
    assert rows[0].details["reason"] == body["reason"]
    assert rows[0].details["erp_document_id"] == "ORPHAN-1"


async def test_a_bill_the_lookup_no_longer_names_is_not_voided(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-006")
    adapter = _Adapter(found="SOMEONE-ELSES")
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(
                f"/api/invoices/{inv_id}/void-erp-orphan-bill",
                json={"erp_document_id": "ORPHAN-1", "reason": "dup"},
            )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "erp_void_not_found"
    assert adapter.voided == []
    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).erp_void_pending is None


async def test_a_paid_orphan_is_refused_by_the_shared_read(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-007")
    adapter = _Adapter(read=ErpInvoiceStatus.paid)
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(
                f"/api/invoices/{inv_id}/void-erp-orphan-bill",
                json={"erp_document_id": "ORPHAN-1", "reason": "dup"},
            )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "erp_void_bill_paid"
    assert adapter.voided == []


async def test_the_retry_door_refuses_while_the_void_is_calling_the_erp(realdb):
    from datetime import UTC, datetime

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-008")
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        inv.erp_void_pending = {
            "attempt_id": "x",
            "exception_id": None,
            "document_id": "ORPHAN-1",
            "source": "orphan",
            "started_at": datetime.now(UTC).isoformat(),
        }
        await s.commit()
    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/invoices/{inv_id}/retry-erp")
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "erp_void_in_progress"
    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.failed


async def test_the_send_door_refuses_while_the_void_is_calling_the_erp(realdb):
    """The first push, not only the retry: `/send-to-erp` from `approved` goes
    through the same `start_erp_send`, and a live void marker (a flag void puts
    the invoice back at `approved` while it calls the ERP, decisions §556)
    refuses it before anything is dispatched."""
    from datetime import UTC, datetime

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-009", status=InvoiceStatus.approved)
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        inv.erp_void_pending = {
            "attempt_id": "x",
            "exception_id": None,
            "document_id": "ORPHAN-1",
            "source": "orphan",
            "started_at": datetime.now(UTC).isoformat(),
        }
        await s.commit()
    with patch("app.services.erp_send.dispatch_erp", new=AsyncMock()) as dispatched:
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(f"/api/invoices/{inv_id}/send-to-erp")
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "erp_void_in_progress"
    dispatched.assert_not_awaited()
    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.approved
