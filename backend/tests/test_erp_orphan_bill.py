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


def test_the_served_verdict_is_absent_off_a_candidate_and_coded_on_one():
    """`erp_orphan_lookup_refusal` (decisions §560): absent unless the invoice
    is a failed push that may have left a bill, so no page offers the lookup
    the server would refuse `erp_lookup_not_failed`; on a candidate, the role,
    then the adapter, then null."""
    from app.services.erp_bill_void import ORPHAN_LOOKUP_VERDICT_KEY as KEY
    from app.services.erp_bill_void import orphan_lookup_verdicts

    failed = SimpleNamespace(status=InvoiceStatus.failed, erp_document_id=None)
    unknown = SimpleNamespace(state_data={"erp_outcome_unknown": True})
    bc = {"erp": {"type": "dynamics_365_bc", "integration_method": "direct"}}
    xero = {"erp": {"type": "xero", "integration_method": "direct"}}
    with patch("app.api.deps.holds_any_permission", return_value=True):
        assert orphan_lookup_verdicts(failed, unknown, _user(), None) == {KEY: None}
        assert orphan_lookup_verdicts(failed, unknown, _user(), bc)[KEY]["code"] == (
            "erp_lookup_unsupported"
        )
        # Xero offers it (§566) unless another invoice shares its key.
        assert orphan_lookup_verdicts(failed, unknown, _user(), xero) == {KEY: None}
        assert orphan_lookup_verdicts(failed, unknown, _user(), xero, key_shared=True)[KEY][
            "code"
        ] == ("erp_lookup_unsupported")
        # Both Sage adapters offer it (§576): their key is our correlation id.
        for sage in ("sage_accounting", "sage_accounting_za"):
            erp = {"erp": {"type": sage, "integration_method": "direct"}}
            assert orphan_lookup_verdicts(failed, unknown, _user(), erp) == {KEY: None}
        # Never pushed / pushed cleanly / holds its own bill / no invoice: absent.
        assert orphan_lookup_verdicts(failed, SimpleNamespace(state_data={}), _user(), None) == {}
        assert orphan_lookup_verdicts(failed, None, _user(), None) == {}
        approved = SimpleNamespace(status=InvoiceStatus.approved, erp_document_id=None)
        assert orphan_lookup_verdicts(approved, unknown, _user(), None) == {}
        own = SimpleNamespace(status=InvoiceStatus.failed, erp_document_id="B")
        assert orphan_lookup_verdicts(own, unknown, _user(), None) == {}
        assert orphan_lookup_verdicts(None, unknown, _user(), None) == {}
    with patch("app.api.deps.holds_any_permission", return_value=False):
        assert orphan_lookup_verdicts(failed, unknown, _user(), None)[KEY]["code"] == (
            "role_not_permitted"
        )
        assert orphan_lookup_verdicts(failed, None, _user(), None) == {}


async def test_an_orphan_void_without_an_invoice_is_a_coded_refusal():
    """No bare assert: a missing invoice is refused, coded, under `-O` too."""
    from app.services.erp_bill_void import VoidRefused, begin_orphan_void

    with pytest.raises(VoidRefused) as refused:
        await begin_orphan_void(
            None,
            invoice=None,
            instance=None,
            user=_user(),
            reason="r",
            document_id="B",
            payload=None,
        )
    assert refused.value.code in {"role_not_permitted", "erp_void_no_bill"}


def test_only_adapters_with_a_lookup_offer_it():
    from app.services.erp_adapters.base import ErpAdapter
    from app.services.erp_bill_void import lookup_supported

    assert ErpAdapter.supports_bill_lookup is False
    assert lookup_supported(None) is True  # the local mock
    assert (
        lookup_supported({"erp": {"type": "quickbooks_online", "integration_method": "direct"}})
        is True
    )
    # Xero and NetSuite offer their push's own match too (decisions §566), and
    # both Sage adapters (§576).
    for erp in ("xero", "netsuite", "sage_accounting", "sage_accounting_za"):
        assert lookup_supported({"erp": {"type": erp, "integration_method": "direct"}}) is True
    assert (
        lookup_supported({"erp": {"type": "dynamics_365_bc", "integration_method": "direct"}})
        is False
    )


def test_only_xeros_key_is_marked_shared():
    """The key a lookup matches on is unique to one invoice everywhere but
    Xero (supplier + number); only Xero's is checked against the tenant's
    other invoices (decisions §566)."""
    from app.services.erp_adapters.dispatcher import adapter_class

    for key in ("mock", "quickbooks_online", "netsuite", "sage_accounting", "sage_accounting_za"):
        assert getattr(adapter_class(key), "bill_lookup_key_shared", False) is False, key
    assert adapter_class("xero").bill_lookup_key_shared is True


async def test_a_sage_key_is_never_checked_against_the_tenant():
    """Both Sage keys carry our correlation id (§576), so no other invoice can
    share one: `shared_lookup_keys` answers empty without a query."""
    from app.services.erp_bill_void import shared_lookup_keys

    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("no query")))
    inv = SimpleNamespace(id=uuid.uuid4(), vendor_id=uuid.uuid4(), invoice_number="INV-1")
    for sage in ("sage_accounting", "sage_accounting_za"):
        settings = {"erp": {"type": sage, "integration_method": "direct"}}
        assert await shared_lookup_keys(db, settings, [inv]) == set()


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


async def _seed(mk, org_id, *, number, status=InvoiceStatus.failed, unknown=True, vendor_id=None):
    from app.services.workflow_engine import get_or_create_workflow_definition

    inv_id = uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=org_id,
            vendor_id=vendor_id,
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


async def test_an_id_the_erp_reports_gone_is_still_identified_first(realdb):
    """decisions §560: the id is the client's, so the lookup runs before the
    already-gone answer. A cancelled bill the lookup does not name is refused
    `erp_void_not_found` and nothing is recorded as this invoice's void."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-009")
    adapter = _Adapter(found=None, read=ErpInvoiceStatus.cancelled)
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(
                f"/api/invoices/{inv_id}/void-erp-orphan-bill",
                json={"erp_document_id": "SOMEONE-ELSES", "reason": "dup"},
            )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "erp_void_not_found"
    assert adapter.lookups == 1
    assert adapter.voided == []
    assert await _audit(mk, inv_id, "invoice.erp_orphan_bill_voided") == []
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert inv.erp_void_pending is None
        inst = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
        assert "erp_orphan_bills_voided" not in inst.state_data


async def test_a_gone_bill_the_lookup_still_names_is_recorded_without_a_void(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number="ORPH-010")
    adapter = _Adapter(read=ErpInvoiceStatus.cancelled)
    with _adapter(adapter):
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(
                f"/api/invoices/{inv_id}/void-erp-orphan-bill",
                json={"erp_document_id": "ORPHAN-1", "reason": "dup"},
            )
    assert res.status_code == 201, res.text
    assert adapter.lookups == 1
    assert adapter.voided == []
    rows = await _audit(mk, inv_id, "invoice.erp_orphan_bill_voided")
    assert len(rows) == 1 and rows[0].details["already_gone"] is True


@pytest.mark.parametrize("role", ["admin", "ap_manager", "ap_clerk", "cfo"])
@pytest.mark.parametrize("supported", [True, False])
async def test_the_served_verdict_agrees_with_the_lookup(realdb, role, supported):
    """Parity (decisions §560): for each role, candidate and not, adapter with
    and without a lookup, the verdict `GET /review-actions` serves is null
    exactly when `GET /erp-orphan-bill` answers 200, carries the code it
    refuses with otherwise, and is absent only where it refuses
    `erp_lookup_not_failed` (or the role first)."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    candidate = await _seed(mk, info.org_id, number=f"ORPH-P-{uuid.uuid4().hex[:6]}")
    clean = await _seed(mk, info.org_id, number=f"ORPH-P-{uuid.uuid4().hex[:6]}", unknown=False)
    with (
        _adapter(_Adapter()),
        patch("app.services.erp_bill_void.lookup_supported", return_value=supported),
    ):
        async with realdb.client(key=TENANT, role=role) as c:
            for inv_id in (candidate, clean):
                verdicts = (await c.get(f"/api/invoices/{inv_id}/review-actions")).json()
                res = await c.get(f"/api/invoices/{inv_id}/erp-orphan-bill")
                # The permission dependency's 403 is the role refusal.
                answered = (
                    "role_not_permitted"
                    if res.status_code == 403
                    else (None if res.status_code == 200 else res.json()["detail"]["code"])
                )
                if "erp_orphan_lookup_refusal" not in verdicts:
                    assert answered in {"erp_lookup_not_failed", "role_not_permitted"}, res.text
                    continue
                verdict = verdicts["erp_orphan_lookup_refusal"]
                assert answered == (verdict["code"] if verdict else None), res.text
    # The candidate always carries the key; the clean push never does.
    async with realdb.client(key=TENANT, role=role) as c:
        with patch("app.services.erp_bill_void.lookup_supported", return_value=supported):
            assert (
                "erp_orphan_lookup_refusal"
                in (await c.get(f"/api/invoices/{candidate}/review-actions")).json()
            )
            assert (
                "erp_orphan_lookup_refusal"
                not in (await c.get(f"/api/invoices/{clean}/review-actions")).json()
            )


async def test_the_exception_row_carries_the_verdict_only_on_a_failed_push(realdb):
    """A duplicate flag on a failed push that may have left a bill carries the
    lookup verdict; one on a never-pushed invoice carries none, so the queue
    shows no panel there (decisions §560)."""
    from app.services.exception_service import create_exception

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    failed = await _seed(mk, info.org_id, number=f"ORPH-X-{uuid.uuid4().hex[:6]}")
    never = await _seed(
        mk,
        info.org_id,
        number=f"ORPH-X-{uuid.uuid4().hex[:6]}",
        status=InvoiceStatus.ready_for_review,
        unknown=False,
    )
    ids = {}
    tag = uuid.uuid4().hex[:6]
    async with mk() as s:
        for inv_id in (failed, never):
            (await s.get(Invoice, inv_id)).invoice_number = f"ORPH-X-{tag}-{inv_id.hex[:4]}"
        await s.commit()
    async with mk() as s:
        for inv_id in (failed, never):
            exc = await create_exception(
                s,
                exception_type="duplicate",
                severity="error",
                description="detector output",
                organization_id=info.org_id,
                invoice=await s.get(Invoice, inv_id),
                raised_by_user_id=None,
            )
            ids[inv_id] = str(exc.id)
        await s.commit()
    async with realdb.client(key=TENANT, role="admin") as c:
        detail = (await c.get(f"/api/exceptions/{ids[failed]}")).json()
        assert detail["erp_orphan_lookup_refusal"] is None
        assert (
            "erp_orphan_lookup_refusal" not in (await c.get(f"/api/exceptions/{ids[never]}")).json()
        )
        listed = (await c.get("/api/exceptions", params={"search": f"ORPH-X-{tag}"})).json()
        by_id = {row["id"]: row for row in listed["items"]}
        assert set(by_id) == set(ids.values())
        assert by_id[ids[failed]]["erp_orphan_lookup_refusal"] is None
        assert "erp_orphan_lookup_refusal" not in by_id[ids[never]]
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.get(f"/api/exceptions/{ids[failed]}")
        # An ap_manager reads the queue but holds no `payment.void`: the row
        # says so, and the page hides the panel on it (`isOffered`).
        assert res.status_code == 200, res.text
        assert res.json()["erp_orphan_lookup_refusal"]["code"] == "role_not_permitted"


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


# ---------------------------------------------------------------------------
# A lookup key shared with another invoice (Xero's supplier + number, §566)
# ---------------------------------------------------------------------------


class _SharedKeyClass:
    supports_bill_lookup = True
    bill_lookup_key_shared = True


def _shared_key():
    return patch(
        "app.services.erp_bill_void._configured_adapter_class", return_value=_SharedKeyClass
    )


async def _vendor(mk, org_id, *, erp_id=None, realm=None):
    from app.models.vendor import Vendor

    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name=f"Shared Key Co {vendor_id.hex[:6]}",
                erp_vendor_id=erp_id,
                erp_link_realm=realm,
            )
        )
        await s.commit()
    return vendor_id


@pytest.mark.parametrize("other", ["same_vendor", "same_erp_contact", "other_status"])
async def test_a_key_another_invoice_shares_is_never_looked_up_or_voided(realdb, other):
    """Xero matches a bill on supplier + number alone, so while another
    invoice of this tenant carries the same number from the same supplier
    (the same vendor, or one linked to the same ERP contact; any status;
    case and spaces aside) the lookup could name THAT invoice's bill. It is
    refused `erp_lookup_unsupported` before any ERP call, the served verdict
    says the same, and the void is refused too."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    contact = f"xero-contact-{uuid.uuid4().hex[:8]}"
    vendor = await _vendor(mk, info.org_id, erp_id=contact, realm="realm-a")
    number = f"SK-{uuid.uuid4().hex[:6]}"
    inv_id = await _seed(mk, info.org_id, number=number, vendor_id=vendor)
    if other == "same_vendor":
        await _seed(mk, info.org_id, number=f"  {number.lower()} ", vendor_id=vendor)
    elif other == "same_erp_contact":
        twin = await _vendor(mk, info.org_id, erp_id=contact, realm="realm-b")
        await _seed(mk, info.org_id, number=number, vendor_id=twin)
    else:
        await _seed(
            mk,
            info.org_id,
            number=number,
            vendor_id=vendor,
            status=InvoiceStatus.done,
            unknown=False,
        )
    adapter = _Adapter()
    with _adapter(adapter), _shared_key():
        async with realdb.client(key=TENANT, role="admin") as c:
            verdicts = (await c.get(f"/api/invoices/{inv_id}/review-actions")).json()
            res = await c.get(f"/api/invoices/{inv_id}/erp-orphan-bill")
            void = await c.post(
                f"/api/invoices/{inv_id}/void-erp-orphan-bill",
                json={"erp_document_id": "ORPHAN-1", "reason": "duplicate in the ERP"},
            )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "erp_lookup_unsupported"
    assert verdicts["erp_orphan_lookup_refusal"]["code"] == "erp_lookup_unsupported"
    assert void.status_code == 409, void.text
    assert void.json()["detail"]["code"] == "erp_lookup_unsupported"
    assert adapter.lookups == 0 and adapter.voided == []
    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).erp_void_pending is None


async def test_a_key_no_other_invoice_shares_is_looked_up(realdb):
    """Another supplier's invoice with the same number, or this supplier's
    with another number, shares nothing: the lookup runs."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor = await _vendor(mk, info.org_id, erp_id=f"xero-contact-{uuid.uuid4().hex[:8]}")
    stranger = await _vendor(mk, info.org_id, erp_id=f"xero-contact-{uuid.uuid4().hex[:8]}")
    number = f"SK-{uuid.uuid4().hex[:6]}"
    inv_id = await _seed(mk, info.org_id, number=number, vendor_id=vendor)
    await _seed(mk, info.org_id, number=number, vendor_id=stranger)
    await _seed(mk, info.org_id, number=f"{number}-2", vendor_id=vendor)
    adapter = _Adapter()
    with _adapter(adapter), _shared_key():
        async with realdb.client(key=TENANT, role="admin") as c:
            verdicts = (await c.get(f"/api/invoices/{inv_id}/review-actions")).json()
            res = await c.get(f"/api/invoices/{inv_id}/erp-orphan-bill")
    assert verdicts["erp_orphan_lookup_refusal"] is None
    assert res.status_code == 200, res.text
    assert res.json()["found"] is True and adapter.lookups == 1


# ---------------------------------------------------------------------------
# The shared-key check reads the contact the lookup sends (decisions §569)
# ---------------------------------------------------------------------------

_XERO_SETTINGS = {
    "erp": {
        "type": "xero",
        "integration_method": "direct",
        "oauth": {"external_tenant_id": "org-x"},
    }
}


async def _kept(mk, org_id, vendor_id, *, realm, erp_id):
    from app.models.vendor import VendorErpLink

    async with mk() as s:
        s.add(
            VendorErpLink(
                organization_id=org_id, vendor_id=vendor_id, realm=realm, erp_vendor_id=erp_id
            )
        )
        await s.commit()


@pytest.mark.parametrize("sharer", ["row_link", "kept_link"])
async def test_the_shared_key_follows_the_contact_the_lookup_sends(realdb, sharer):
    """V1's row link is D in another company, and C is the link kept for the
    connected one, so the lookup sends ContactIDs=C. Another vendor holding C
    (as its row link, or kept under any company) with an invoice of the same
    number shares the key: reading only V1's row link (D) missed it, and the
    lookup could have named, and the void voided, that invoice's bill."""
    from app.services.erp_bill_void import shared_lookup_keys
    from app.services.erp_link_realm import connection_realm

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    realm = connection_realm(_XERO_SETTINGS["erp"])
    c_id = f"xero-c-{uuid.uuid4().hex[:8]}"
    v1 = await _vendor(mk, info.org_id, erp_id=f"xero-d-{uuid.uuid4().hex[:8]}", realm="xero:org-y")
    await _kept(mk, info.org_id, v1, realm=realm, erp_id=c_id)
    if sharer == "row_link":
        v2 = await _vendor(mk, info.org_id, erp_id=c_id, realm=realm)
    else:
        v2 = await _vendor(mk, info.org_id, erp_id=f"xero-e-{uuid.uuid4().hex[:8]}", realm=realm)
        await _kept(mk, info.org_id, v2, realm="xero:org-z", erp_id=c_id)
    number = f"INV-{uuid.uuid4().hex[:6]}"
    await _seed(mk, info.org_id, number=number, vendor_id=v2, status=InvoiceStatus.done)
    mine = await _seed(mk, info.org_id, number=number, vendor_id=v1)
    lone = await _seed(mk, info.org_id, number=f"{number}-LONE", vendor_id=v1)
    async with mk() as s:
        invoices = [await s.get(Invoice, mine), await s.get(Invoice, lone)]
        assert await shared_lookup_keys(s, _XERO_SETTINGS, invoices) == {mine}


async def test_the_shared_key_check_is_a_bounded_number_of_queries(realdb):
    """A page of exceptions asks for every failed push at once: the check is
    the same handful of statements for one invoice as for many."""
    from app.services.erp_bill_void import shared_lookup_keys
    from tests.query_counter import QueryCounter

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    ids = []
    for _ in range(6):
        vendor = await _vendor(mk, info.org_id, erp_id=f"xero-q-{uuid.uuid4().hex[:8]}")
        ids.append(
            await _seed(mk, info.org_id, number=f"QC-{uuid.uuid4().hex[:6]}", vendor_id=vendor)
        )
    async with mk() as s:
        invoices = [await s.get(Invoice, i) for i in ids]
        with QueryCounter() as one:
            await shared_lookup_keys(s, _XERO_SETTINGS, invoices[:1])
        with QueryCounter() as many:
            await shared_lookup_keys(s, _XERO_SETTINGS, invoices)
    assert len(many) == len(one) <= 3


async def test_the_exception_list_and_detail_carry_the_shared_key_refusal(realdb):
    """The queue serves the same verdict the lookup gives: a duplicate flag on
    a failed push whose key another invoice shares says
    `erp_lookup_unsupported`, on the list row and the detail alike."""
    from app.services.exception_service import create_exception

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor = await _vendor(mk, info.org_id, erp_id=f"xero-contact-{uuid.uuid4().hex[:8]}")
    tag = uuid.uuid4().hex[:6]
    number = f"ORPH-S-{tag}"
    failed = await _seed(mk, info.org_id, number=number, vendor_id=vendor)
    await _seed(mk, info.org_id, number=number, vendor_id=vendor, status=InvoiceStatus.done)
    async with mk() as s:
        exc = await create_exception(
            s,
            exception_type="duplicate",
            severity="error",
            description="detector output",
            organization_id=info.org_id,
            invoice=await s.get(Invoice, failed),
            raised_by_user_id=None,
        )
        exc_id = str(exc.id)
        await s.commit()
    with _adapter(_Adapter()), _shared_key():
        async with realdb.client(key=TENANT, role="admin") as c:
            detail = (await c.get(f"/api/exceptions/{exc_id}")).json()
            listed = (await c.get("/api/exceptions", params={"search": number})).json()
    assert detail["erp_orphan_lookup_refusal"]["code"] == "erp_lookup_unsupported"
    row = next(r for r in listed["items"] if r["id"] == exc_id)
    assert row["erp_orphan_lookup_refusal"]["code"] == "erp_lookup_unsupported"


# ---------------------------------------------------------------------------
# The push applies the same shared-key rule before adopting a bill (§572)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shared", [True, False])
async def test_the_push_tells_the_adapter_when_its_key_is_shared(realdb, shared):
    """`_call_erp` asks `shared_lookup_keys` (the orphan lookup's own rule) for
    the configured Xero connection and hands the verdict to the adapter as
    `payload.bill_key_shared`, which refuses to adopt a bill found on the key.
    The real Xero class is resolved from the config, not a stand-in, so the
    flag on `XeroAdapter` is what turns the check on."""
    from app.services.erp import _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor = await _vendor(mk, info.org_id, erp_id=f"xero-push-{uuid.uuid4().hex[:8]}")
    number = f"PUSH-{uuid.uuid4().hex[:6]}"
    mine = await _seed(mk, info.org_id, number=number, vendor_id=vendor)
    await _seed(
        mk,
        info.org_id,
        number=number if shared else f"{number}-OTHER",
        vendor_id=vendor,
        status=InvoiceStatus.rejected,
        unknown=False,
    )
    seen: list[bool] = []

    class _Capture:
        async def post_invoice(self, payload):
            seen.append(payload.bill_key_shared)
            return ErpPostResult(success=True, erp_document_id="XERO-BILL-1")

    async with mk() as s:
        inv = await s.get(Invoice, mine)
        inv.status = InvoiceStatus.approved
        with patch("app.services.erp.get_erp_adapter", return_value=_Capture()):
            await _call_erp(s, inv, dict(_XERO_SETTINGS["erp"]))
    assert seen == [shared]


async def test_the_push_asks_nothing_for_an_adapter_whose_key_is_its_own(realdb):
    """Every other adapter's key is unique to one invoice: no shared-key query
    runs and the flag stays False (§572)."""
    from app.services.erp_bill_void import shared_lookup_keys
    from tests.query_counter import QueryCounter

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor = await _vendor(mk, info.org_id, erp_id=f"qbo-{uuid.uuid4().hex[:8]}")
    number = f"OWN-{uuid.uuid4().hex[:6]}"
    mine = await _seed(mk, info.org_id, number=number, vendor_id=vendor)
    await _seed(mk, info.org_id, number=number, vendor_id=vendor, status=InvoiceStatus.done)
    async with mk() as s:
        inv = await s.get(Invoice, mine)
        with QueryCounter() as queries:
            found = await shared_lookup_keys(
                s, {"erp": {"type": "mock", "integration_method": "direct"}}, [inv]
            )
    assert found == set() and len(queries) == 0
