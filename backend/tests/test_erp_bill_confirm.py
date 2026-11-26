"""A bill on a shared Xero key, confirmed by a person as this invoice's
(decisions §587).

Pinned here:

* the verdict (`erp_bill_confirm_refusal`): offered only on a failed push
  whose last refusal was `erp_bill_key_shared`, by an ERP whose key may be
  shared, to a holder of `payment.void` with a role that may retry the push;
* `POST /api/invoices/{id}/confirm-erp-bill` records the bill once (state,
  append-only audit row with actor and reason) and starts the retry;
* a second invoice cannot claim the same bill, whether the first holds it
  as its bill of record or only by a confirmation;
* a bill the lookup no longer names is refused, and nothing is recorded;
* `_call_erp` hands the confirmation to the adapter, and the push's last
  refusal code is recorded on failure and dropped when a new push starts.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import WorkflowInstance
from app.services import erp_bill_confirm as ebc
from tests.test_erp_orphan_bill import (
    TENANT,
    _Adapter,
    _adapter,
    _audit,
    _seed,
    _shared_key,
    _vendor,
)

# ---------------------------------------------------------------------------
# DB-free: the verdict
# ---------------------------------------------------------------------------


def _user(perms=("payment.void",), roles=("admin",)):
    return SimpleNamespace(
        id=uuid.uuid4(),
        effective_permissions=frozenset(perms),
        roles=[SimpleNamespace(name=r) for r in roles],
    )


def _inv(status=InvoiceStatus.failed, doc=None):
    return SimpleNamespace(status=status, erp_document_id=doc, approved_by="Approver")


def _inst(code=ebc.ERP_BILL_KEY_SHARED):
    return SimpleNamespace(state_data={ebc.LAST_REFUSAL_CODE_KEY: code} if code else {})


def test_only_a_push_refused_on_a_shared_key_is_a_candidate():
    assert ebc.confirm_candidate(_inv(), _inst())
    assert not ebc.confirm_candidate(_inv(), _inst("vendor_not_linked"))
    assert not ebc.confirm_candidate(_inv(), _inst(None))
    assert not ebc.confirm_candidate(_inv(doc="B-1"), _inst())
    assert not ebc.confirm_candidate(_inv(InvoiceStatus.approved), _inst())
    assert not ebc.confirm_candidate(None, _inst())


def test_the_verdict_is_absent_off_a_candidate():
    assert ebc.confirm_verdicts(_inv(), _inst("vendor_not_linked"), _user(), {}) == {}


@pytest.mark.parametrize(
    "perms, roles",
    [
        ((), ("admin",)),  # a role that may retry, without payment.void
        (("payment.void",), ("ap_clerk",)),  # payment.void, no retry role
    ],
)
def test_the_verdict_needs_payment_void_and_a_retry_role(perms, roles):
    with _shared_key():
        verdict = ebc.confirm_verdicts(_inv(), _inst(), _user(perms, roles), {})
    assert verdict[ebc.CONFIRM_VERDICT_KEY]["code"] == "role_not_permitted"


def test_the_verdict_is_null_for_a_shared_key_erp_and_refused_otherwise():
    with _shared_key():
        assert ebc.confirm_verdicts(_inv(), _inst(), _user(), {}) == {ebc.CONFIRM_VERDICT_KEY: None}
    # The local mock's key is its own: nothing to confirm.
    refused = ebc.confirm_verdicts(_inv(), _inst(), _user(), {})[ebc.CONFIRM_VERDICT_KEY]
    assert refused["code"] == ebc.ERP_BILL_CONFIRM_NOT_OFFERED


def test_a_confirmed_bill_id_is_read_strictly():
    key = ebc.CONFIRMED_BILL_KEY
    assert ebc.confirmed_bill_id({key: {"document_id": " B-1 "}}) == "B-1"
    assert ebc.confirmed_bill_id({key: {"document_id": ""}}) is None
    assert ebc.confirmed_bill_id({key: "B-1"}) is None
    assert ebc.confirmed_bill_id(None) is None


# ---------------------------------------------------------------------------
# Against a real tenant
# ---------------------------------------------------------------------------


async def _pair(mk, org_id):
    """A failed push refused `erp_bill_key_shared`, and the rejected invoice
    sharing its supplier + number."""
    vendor = await _vendor(mk, org_id, erp_id=f"xero-c-{uuid.uuid4().hex[:8]}")
    number = f"CONF-{uuid.uuid4().hex[:6]}"
    mine = await _seed(mk, org_id, number=number, vendor_id=vendor)
    other = await _seed(
        mk, org_id, number=number, vendor_id=vendor, status=InvoiceStatus.rejected, unknown=False
    )
    await _set_code(mk, mine)
    return mine, other


async def _set_code(mk, inv_id, code=ebc.ERP_BILL_KEY_SHARED):
    async with mk() as s:
        inst = await _instance(s, inv_id)
        inst.state_data = {**(inst.state_data or {}), ebc.LAST_REFUSAL_CODE_KEY: code}
        await s.commit()


async def _instance(s, inv_id):
    return (
        await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
    ).scalar_one()


def _no_dispatch():
    return patch("app.services.erp_send.dispatch_erp", new=AsyncMock())


_BODY = {"erp_document_id": "ORPHAN-1", "reason": "Our earlier push created it; INV-x is a dup"}


async def test_the_confirmation_records_the_bill_once_and_retries(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, _ = await _pair(mk, info.org_id)
    adapter = _Adapter()
    with _adapter(adapter), _shared_key(), _no_dispatch() as dispatched:
        async with realdb.client(key=TENANT, role="admin") as c:
            verdicts = (await c.get(f"/api/invoices/{mine}/review-actions")).json()
            looked = await c.get(f"/api/invoices/{mine}/erp-bill-confirmation")
            res = await c.post(f"/api/invoices/{mine}/confirm-erp-bill", json=_BODY)
    assert verdicts[ebc.CONFIRM_VERDICT_KEY] is None
    assert looked.status_code == 200, looked.text
    assert looked.json()["bill"]["erp_document_id"] == "ORPHAN-1"
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "sending_to_erp"
    dispatched.assert_awaited_once()
    # The lookup ran for the GET and again inside the confirmation.
    assert adapter.lookups == 2 and adapter.voided == []
    async with mk() as s:
        state = (await _instance(s, mine)).state_data
        assert ebc.confirmed_bill_id(state) == "ORPHAN-1"
        # A new push drops the last failure's code.
        assert ebc.LAST_REFUSAL_CODE_KEY not in state
        assert (await s.get(Invoice, mine)).status == InvoiceStatus.sending_to_erp
    rows = await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMED)
    assert len(rows) == 1
    assert rows[0].details["erp_document_id"] == "ORPHAN-1"
    assert rows[0].details["reason"] == _BODY["reason"]
    assert rows[0].actor_id is not None


@pytest.mark.parametrize("held_by", ["confirmation", "bill_of_record"])
async def test_a_second_invoice_cannot_claim_the_same_bill(realdb, held_by):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, other = await _pair(mk, info.org_id)
    async with mk() as s:
        if held_by == "confirmation":
            inst = await _instance(s, other)
            inst.state_data = {
                **(inst.state_data or {}),
                ebc.CONFIRMED_BILL_KEY: {"document_id": "ORPHAN-1"},
            }
        else:
            (await s.get(Invoice, other)).erp_document_id = "ORPHAN-1"
        await s.commit()
    adapter = _Adapter()
    with _adapter(adapter), _shared_key(), _no_dispatch() as dispatched:
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(f"/api/invoices/{mine}/confirm-erp-bill", json=_BODY)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == ebc.ERP_BILL_CLAIMED
    assert adapter.lookups == 0
    dispatched.assert_not_awaited()
    async with mk() as s:
        assert ebc.confirmed_bill_id((await _instance(s, mine)).state_data) is None
        assert (await s.get(Invoice, mine)).status == InvoiceStatus.failed
    assert await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMED) == []


async def test_after_one_confirms_the_sharer_is_refused(realdb):
    """Both invoices failed on the shared key; the first confirmation wins and
    the second is refused, so one bill reaches one invoice."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, other = await _pair(mk, info.org_id)
    async with mk() as s:
        (await s.get(Invoice, other)).status = InvoiceStatus.failed
        await s.commit()
    await _set_code(mk, other)
    with _adapter(_Adapter()), _shared_key(), _no_dispatch():
        async with realdb.client(key=TENANT, role="admin") as c:
            first = await c.post(f"/api/invoices/{mine}/confirm-erp-bill", json=_BODY)
            second = await c.post(f"/api/invoices/{other}/confirm-erp-bill", json=_BODY)
    assert first.status_code == 202, first.text
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == ebc.ERP_BILL_CLAIMED


async def test_a_bill_the_lookup_no_longer_names_is_refused(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, _ = await _pair(mk, info.org_id)
    for found in ("SOMEONE-ELSES", None):
        with _adapter(_Adapter(found=found)), _shared_key(), _no_dispatch() as dispatched:
            async with realdb.client(key=TENANT, role="admin") as c:
                res = await c.post(f"/api/invoices/{mine}/confirm-erp-bill", json=_BODY)
        assert res.status_code == 409, res.text
        assert res.json()["detail"]["code"] == ebc.ERP_BILL_CONFIRM_NOT_FOUND
        dispatched.assert_not_awaited()
    async with mk() as s:
        assert ebc.confirmed_bill_id((await _instance(s, mine)).state_data) is None
    assert await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMED) == []


async def test_rbac_and_the_candidate_gate(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, _ = await _pair(mk, info.org_id)
    other_failure = await _seed(mk, info.org_id, number=f"CONF-{uuid.uuid4().hex[:6]}")
    await _set_code(mk, other_failure, "vendor_not_linked")
    adapter = _Adapter()
    with _adapter(adapter), _shared_key(), _no_dispatch():
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            assert (await c.get(f"/api/invoices/{mine}/erp-bill-confirmation")).status_code == 403
            res = await c.post(f"/api/invoices/{mine}/confirm-erp-bill", json=_BODY)
            assert res.status_code == 403
        async with realdb.client(key=TENANT, role="admin") as c:
            verdicts = (await c.get(f"/api/invoices/{other_failure}/review-actions")).json()
            assert ebc.CONFIRM_VERDICT_KEY not in verdicts
            res = await c.post(f"/api/invoices/{other_failure}/confirm-erp-bill", json=_BODY)
            assert res.status_code == 409
            assert res.json()["detail"]["code"] == ebc.ERP_BILL_CONFIRM_NOT_OFFERED
            res = await c.post(
                f"/api/invoices/{mine}/confirm-erp-bill",
                json={"erp_document_id": "ORPHAN-1", "reason": "   "},
            )
            assert res.status_code == 422
    assert adapter.lookups == 0


async def test_the_push_hands_the_confirmation_to_the_adapter(realdb):
    from app.services.erp import _call_erp
    from app.services.erp_adapters.base import ErpPostResult

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, _ = await _pair(mk, info.org_id)
    seen: list = []

    class _Capture:
        async def post_invoice(self, payload):
            seen.append((payload.bill_key_shared, payload.confirmed_bill_id))
            return ErpPostResult(success=True, erp_document_id="ORPHAN-1")

    async with mk() as s:
        inst = await _instance(s, mine)
        inst.state_data = {
            **(inst.state_data or {}),
            ebc.CONFIRMED_BILL_KEY: {"document_id": "ORPHAN-1"},
        }
        await s.commit()
        inv = await s.get(Invoice, mine)
        xero = {
            "type": "xero",
            "integration_method": "direct",
            "oauth": {"external_tenant_id": "o"},
        }
        with patch("app.services.erp.get_erp_adapter", return_value=_Capture()):
            await _call_erp(s, inv, xero, instance=inst)
        assert inv.erp_document_id == "ORPHAN-1"
    assert seen == [(True, "ORPHAN-1")]


async def test_a_final_failure_records_its_refusal_code(realdb):
    """`send_to_erp_internal` writes the coded refusal of the failure it
    lands, which is what the verdict reads."""
    from app.services.erp import send_to_erp_internal
    from app.services.erp_adapters.base import ErpPostResult

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed(mk, info.org_id, number=f"CODE-{uuid.uuid4().hex[:6]}")

    class _Refuse:
        async def post_invoice(self, payload):
            return ErpPostResult(
                success=False,
                retryable=False,
                message="Xero post refused: erp_bill_key_shared",
                refusal_code="erp_bill_key_shared",
            )

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        inv.status = InvoiceStatus.sending_to_erp
        inst = await _instance(s, inv_id)
        inst.state_data = {**(inst.state_data or {}), "erp_retries": 0}
        await s.commit()
        with patch("app.services.erp.get_erp_adapter", return_value=_Refuse()):
            await send_to_erp_internal(s, inv)
    async with mk() as s:
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.failed
        state = (await _instance(s, inv_id)).state_data
        assert state[ebc.LAST_REFUSAL_CODE_KEY] == "erp_bill_key_shared"


# ---------------------------------------------------------------------------
# Two invoices confirming one bill serialize on the per-bill lock; a
# confirmation stops standing once used, refuted or its invoice is rejected
# (decisions §590)
# ---------------------------------------------------------------------------


async def _advisory_waiters(mk) -> int:
    from sqlalchemy import text

    async with mk() as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                )
            )
        ).scalar_one()


async def test_a_confirmation_waits_for_another_holding_the_bill_then_is_refused(realdb):
    """A holds the per-bill lock with its confirmation uncommitted. C's
    confirmation passes the unlocked pre-check (A's write is invisible),
    blocks on the lock, and once A commits answers ``erp_bill_claimed`` with
    nothing recorded."""
    import asyncio

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, other = await _pair(mk, info.org_id)
    async with mk() as s:
        (await s.get(Invoice, other)).status = InvoiceStatus.failed
        await s.commit()
    await _set_code(mk, other)
    with _adapter(_Adapter()), _shared_key(), _no_dispatch() as dispatched:
        async with mk() as a:
            await ebc.lock_bill(a, "ORPHAN-1")
            inst = await _instance(a, other)
            inst.state_data = {
                **(inst.state_data or {}),
                ebc.CONFIRMED_BILL_KEY: {"document_id": "ORPHAN-1"},
            }
            await a.flush()
            async with realdb.client(key=TENANT, role="admin") as c:
                task = asyncio.create_task(
                    c.post(f"/api/invoices/{mine}/confirm-erp-bill", json=_BODY)
                )
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 10
                while await _advisory_waiters(mk) == 0 and loop.time() < deadline:
                    assert not task.done(), (await task).text
                    await asyncio.sleep(0.02)
                assert await _advisory_waiters(mk) == 1  # C is blocked on A's lock
                assert not task.done()
                await a.commit()
                res = await task
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == ebc.ERP_BILL_CLAIMED
    dispatched.assert_not_awaited()
    async with mk() as s:
        assert ebc.confirmed_bill_id((await _instance(s, mine)).state_data) is None
    assert await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMED) == []


async def _confirmed(mk, inv_id, doc="ORPHAN-1"):
    async with mk() as s:
        inst = await _instance(s, inv_id)
        inst.state_data = {
            **(inst.state_data or {}),
            ebc.CONFIRMED_BILL_KEY: {"document_id": doc},
        }
        await s.commit()


async def _push(mk, inv_id, result):
    from app.services.erp import ErpPostRefusedError, _call_erp

    class _Fixed:
        async def post_invoice(self, payload):
            return result

    xero = {"type": "xero", "integration_method": "direct", "oauth": {"external_tenant_id": "o"}}
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        inst = await _instance(s, inv_id)
        with patch("app.services.erp.get_erp_adapter", return_value=_Fixed()):
            try:
                await _call_erp(s, inv, xero, instance=inst)
            except ErpPostRefusedError:
                pass
        await s.commit()


async def test_a_confirmation_is_withdrawn_when_its_invoice_adopts_another_bill(realdb):
    from app.services.erp_adapters.base import ErpPostResult

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, other = await _pair(mk, info.org_id)
    await _confirmed(mk, mine)
    await _push(mk, mine, ErpPostResult(success=True, erp_document_id="BILL-NEW"))
    async with mk() as s:
        assert ebc.CONFIRMED_BILL_KEY not in (await _instance(s, mine)).state_data
    [row] = await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMATION_WITHDRAWN)
    assert row.details["erp_document_id"] == "ORPHAN-1"
    assert row.details["reason"] == ebc.WITHDRAWN_OTHER_BILL
    # ORPHAN-1 is free for its real owner again.
    async with mk() as s:
        assert not await ebc.claimed_elsewhere(s, other, "ORPHAN-1")


async def test_a_confirmation_used_by_the_push_is_dropped_silently(realdb):
    from app.services.erp_adapters.base import ErpPostResult

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, other = await _pair(mk, info.org_id)
    await _confirmed(mk, mine)
    await _push(mk, mine, ErpPostResult(success=True, erp_document_id="ORPHAN-1"))
    async with mk() as s:
        assert ebc.CONFIRMED_BILL_KEY not in (await _instance(s, mine)).state_data
        assert (await s.get(Invoice, mine)).erp_document_id == "ORPHAN-1"
        # The bill of record carries the claim now.
        assert await ebc.claimed_elsewhere(s, other, "ORPHAN-1")
    assert await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMATION_WITHDRAWN) == []


async def test_a_confirmation_refused_for_a_changed_total_is_withdrawn(realdb):
    from app.services.erp_adapters.base import erp_refusal

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, other = await _pair(mk, info.org_id)
    await _confirmed(mk, mine)
    await _push(mk, mine, erp_refusal("Xero", "duplicate_document_number"))
    async with mk() as s:
        assert ebc.CONFIRMED_BILL_KEY not in (await _instance(s, mine)).state_data
        assert not await ebc.claimed_elsewhere(s, other, "ORPHAN-1")
    [row] = await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMATION_WITHDRAWN)
    assert row.details["reason"] == ebc.WITHDRAWN_TOTAL_CHANGED


async def test_another_refusal_keeps_the_confirmation(realdb):
    from app.services.erp_adapters.base import erp_refusal

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, _ = await _pair(mk, info.org_id)
    await _confirmed(mk, mine)
    await _push(mk, mine, erp_refusal("Xero", "erp_bill_key_shared"))
    async with mk() as s:
        assert ebc.confirmed_bill_id((await _instance(s, mine)).state_data) == "ORPHAN-1"


async def test_a_rejected_invoice_no_longer_claims_its_confirmed_bill(realdb):
    from app.services.workflow_engine import transition_invoice

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine, other = await _pair(mk, info.org_id)
    async with mk() as s:
        (await s.get(Invoice, other)).status = InvoiceStatus.failed
        await s.commit()
    await _set_code(mk, other)
    await _confirmed(mk, mine)
    actor = uuid.uuid4()
    async with mk() as s:
        inv = await s.get(Invoice, mine)
        inv.status = InvoiceStatus.ready_for_review
        await s.flush()
        await transition_invoice(
            s, inv, InvoiceStatus.rejected, actor_id=actor, action_name="invoice.rejected"
        )
        await s.commit()
    async with mk() as s:
        assert ebc.CONFIRMED_BILL_KEY not in (await _instance(s, mine)).state_data
    [row] = await _audit(mk, mine, ebc.ACTION_INVOICE_ERP_BILL_CONFIRMATION_WITHDRAWN)
    assert row.details["reason"] == ebc.WITHDRAWN_REJECTED
    assert row.actor_id == actor
    # The real owner confirms it now.
    with _adapter(_Adapter()), _shared_key(), _no_dispatch():
        async with realdb.client(key=TENANT, role="admin") as c:
            res = await c.post(f"/api/invoices/{other}/confirm-erp-bill", json=_BODY)
    assert res.status_code == 202, res.text
