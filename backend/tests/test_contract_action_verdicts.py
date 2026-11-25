"""Contract rows carry the caller's verdict on each lifecycle action, and it
says exactly what the endpoint answers (decisions §507).

`actions.{activate,terminate,cancel,renew,create_po,delete}`. The web modal
restated the status rules — and had drifted: it hid Activate and Terminate on
an `expired` contract and Create PO on a `draft`, all of which the endpoints
accept — and the bulk "Change status" offered every action whatever was
selected.

The parity property, per role × status × action: verdict `None` exactly when
the endpoint succeeds, otherwise exactly the coded detail it answers with
(only `require_roles`' own 403 stays a plain string). The list, the detail and
the mutation response carry the same verdict shape. The unit tests at the top
pin the gate table itself and run without a database; the parity tests need
the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.contracts import contract_action_refusals
from app.models.contract import Contract, ContractStatus
from app.models.entity import Entity
from app.models.vendor import Vendor
from app.services.contract_controls import (
    CONTRACT_DELETE_REFUSED,
    CONTRACT_STATUS_REFUSED,
    contract_create_po_refusal,
    contract_delete_refusal,
    contract_renew_refusal,
    contract_transition_refusal,
)

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
ACTIONS = ("activate", "terminate", "cancel", "renew", "create_po", "delete")
STATES = tuple(s.value for s in ContractStatus)

#: The allowed starting statuses, stated once here as the expectation the
#: gates are checked against (the page used to hold a drifted copy).
ALLOWED = {
    "activate": {"draft", "expired"},
    "terminate": {"active", "expired"},
    "cancel": {"draft", "active"},
    "renew": {"draft", "active", "expired"},
    "create_po": {"draft", "active"},
    "delete": {"draft", "cancelled"},
}


def _gate(action: str, contract):
    if action in ("activate", "terminate", "cancel"):
        return contract_transition_refusal(contract, action)
    return {
        "renew": contract_renew_refusal,
        "create_po": contract_create_po_refusal,
        "delete": contract_delete_refusal,
    }[action](contract)


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("action", ACTIONS)
def test_each_gate_allows_exactly_its_starting_statuses(action, state):
    contract = SimpleNamespace(status=ContractStatus(state))
    refusal = _gate(action, contract)
    if state in ALLOWED[action]:
        assert refusal is None
        return
    assert refusal is not None
    assert refusal.http_status == 409
    verdict = refusal.verdict()
    if action == "delete":
        assert verdict["code"] == CONTRACT_DELETE_REFUSED
        assert verdict["params"] == {"status": state}
    else:
        assert verdict["code"] == CONTRACT_STATUS_REFUSED
        assert verdict["params"] == {"status": state, "action": action}
    # The endpoint answers with the very same coded detail.
    assert refusal.http().detail == verdict


@pytest.mark.parametrize("role", ROLES)
def test_a_role_that_cannot_manage_contracts_is_refused_every_action(role):
    user = SimpleNamespace(id=uuid.uuid4(), roles=[SimpleNamespace(name=role)])
    actions = contract_action_refusals(SimpleNamespace(status=ContractStatus.draft), user)
    for action in ACTIONS:
        verdict = getattr(actions, action)
        if role in ("admin", "ap_manager"):
            expected_none = "draft" in ALLOWED[action]
            assert (verdict is None) == expected_none, (role, action, verdict)
        else:
            assert verdict is not None and verdict.code == "role_not_permitted", (role, action)


# ── Parity against the endpoints (real DB) ──────────────────────────────────


async def _contract(realdb, *, status: str) -> uuid.UUID:
    org_id = realdb.info(TENANT).org_id
    contract_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        entity_id = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        vendor = Vendor(organization_id=org_id, name=f"Verdict Vendor {contract_id.hex[:6]}")
        vendor.entity_id = entity_id
        s.add(vendor)
        await s.flush()
        s.add(
            Contract(
                id=contract_id,
                contract_number=f"CV-{contract_id.hex[:8]}",
                title="Parity contract",
                vendor_id=vendor.id,
                currency="USD",
                total_value=Decimal("1000.00"),
                status=ContractStatus(status),
                organization_id=org_id,
                entity_id=entity_id,
            )
        )
        await s.commit()
    return contract_id


async def _act(c, contract_id, action):
    base = f"/api/contracts/{contract_id}"
    if action == "delete":
        return await c.delete(base)
    if action == "renew":
        return await c.post(f"{base}/renew", json={"end_date": "2099-12-31"})
    if action == "create_po":
        return await c.post(f"{base}/create-po", json={})
    return await c.post(f"{base}/{action}")


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code in (200, 201, 204), (label, resp.text)
        return
    if verdict["code"] == "role_not_permitted":
        assert resp.status_code == 403, (label, resp.text)
        return
    assert resp.status_code == 409, (label, verdict, resp.text)
    assert resp.json()["detail"] == verdict, (label, verdict, resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ROLES)
async def test_the_contract_verdicts_match_every_lifecycle_endpoint(realdb, role):
    seen = set()
    for state in STATES:
        for action in ACTIONS:
            contract_id = await _contract(realdb, status=state)
            async with realdb.client(key=TENANT, role=role) as c:
                listed = await c.get("/api/contracts", params={"page_size": 100})
                detail = await c.get(f"/api/contracts/{contract_id}")
                assert listed.status_code == 200, listed.text
                assert detail.status_code == 200, detail.text
                row = {i["id"]: i for i in listed.json()["items"]}[str(contract_id)]
                assert row["actions"] == detail.json()["actions"]
                verdict = row["actions"][action]
                resp = await _act(c, contract_id, action)
            _assert_parity(verdict, resp, (role, state, action))
            if verdict is not None:
                seen.add(verdict["code"])
            elif action in ("activate", "terminate", "cancel", "renew"):
                # A mutation response carries the verdicts too, so the modal
                # re-reads its buttons from what it just got back.
                assert set(resp.json()["actions"]) == set(ACTIONS)
    if role in ("admin", "ap_manager"):
        assert {CONTRACT_STATUS_REFUSED, CONTRACT_DELETE_REFUSED} <= seen
    else:
        assert seen == {"role_not_permitted"}


@pytest.mark.asyncio
async def test_renewing_a_draft_stamps_activated_at(realdb):
    """Renew puts a draft in force without passing through activate, so it
    stamps `activated_at` as activate does (decisions §507)."""
    contract_id = await _contract(realdb, status="draft")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/contracts/{contract_id}/renew", json={"end_date": "2099-12-31"})
    assert resp.status_code == 200, resp.text
    async with realdb.sessionmaker(TENANT)() as s:
        contract = await s.get(Contract, contract_id)
        assert contract.status == ContractStatus.active
        assert contract.activated_at is not None


@pytest.mark.asyncio
async def test_bulk_status_skips_with_the_gates_sentence(realdb):
    contract_id = await _contract(realdb, status="terminated")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/contracts/bulk/status", json={"ids": [str(contract_id)], "action": "activate"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == 0
    assert body["skipped"] == [
        {"id": str(contract_id), "reason": "Cannot activate a contract in 'terminated' status"}
    ]


@pytest.mark.asyncio
# Multi-entity is plan-gated (decisions §258): a second entity needs Scale.
@pytest.mark.plan("scale")
async def test_bulk_status_skips_a_contract_outside_the_entity_selector(realdb):
    """`bulk/status` applies the single-row endpoints' entity check per row:
    a contract in another entity is skipped as "Contract not found" and left
    unchanged, exactly as `POST /{id}/activate` answers it."""
    from tests.entity_scope_probe import two_entities

    contract_id = await _contract(realdb, status="draft")  # in the default entity
    async with realdb.client(key=TENANT, role="admin") as c:
        _default_id, other_id = await two_entities(c, slug=f"cv-{uuid.uuid4().hex[:8]}")
        single = await c.post(
            f"/api/contracts/{contract_id}/activate", headers={"X-Entity-ID": other_id}
        )
        resp = await c.post(
            "/api/contracts/bulk/status",
            json={"ids": [str(contract_id)], "action": "activate"},
            headers={"X-Entity-ID": other_id},
        )
    assert single.status_code == 404
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "updated": 0,
        "skipped": [{"id": str(contract_id), "reason": single.json()["detail"]}],
    }
    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.get(Contract, contract_id)).status == ContractStatus.draft
