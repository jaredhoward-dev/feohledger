"""`api/organization_match_rules` — the audited writer of `settings.matching`
(decisions §512): the organization tolerance / inspection layer and the
per-vendor and per-commodity (header GL account) rules.

Covers:

  * Validation — the coded refusals for a tolerance that is not a number, out
    of 0–100, or finer than two places; an empty rule; an unknown vendor / GL.
  * Every stored value round-trips through the resolver to the same Decimal,
    so what the screen saves is what the PO match applies.
  * RBAC — admin-only on read and write.
  * Audit first: a rule change writes `organization.match_rule_updated` /
    `_removed` with old and new; a no-op writes nothing; an unwritable audit
    row is a 503 with nothing saved.
  * The generic `PATCH /api/organization` refuses a change to the block and
    passes an unchanged copy.
  * A stored tolerance outside 0–100 (an old unaudited PATCH) reads back as
    stored with `tolerance_out_of_range: true` and is never rewritten.

`Organization.settings` is reset by the `realdb` harness before every test.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.api.organization_match_rules import (
    guard_matching_block,
    parse_tolerance,
    tolerance_out_of_range,
)
from app.models.gl_account import GLAccount
from app.models.organization import Organization
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.matching_rules import resolve_match_rule

RULES = "/api/organization/match-rules"


# ---------- pure helpers (no database) ---------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2.5", Decimal("2.5")), (0, Decimal("0")), ("100", Decimal("100")), (1.25, Decimal("1.25"))],
)
def test_parse_tolerance_accepts_exact_values(raw, expected):
    assert parse_tolerance(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("abc", "match_tolerance_invalid"),
        ("NaN", "match_tolerance_invalid"),
        ("Infinity", "match_tolerance_invalid"),
        (True, "match_tolerance_invalid"),
        ("-0.01", "match_tolerance_out_of_range"),
        ("100.01", "match_tolerance_out_of_range"),
        ("1.005", "match_tolerance_too_precise"),
    ],
)
def test_parse_tolerance_refuses_with_a_code(raw, code):
    with pytest.raises(HTTPException) as exc:
        parse_tolerance(raw)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == code


def test_parse_tolerance_none_means_inherit():
    assert parse_tolerance(None) is None


def test_guard_passes_an_absent_or_unchanged_block():
    stored = {"matching": {"tolerance_pct": "2", "vendor_rules": {}}}
    guard_matching_block({"fraud_rules": {}}, stored)
    guard_matching_block({"matching": {"tolerance_pct": "2", "vendor_rules": {}}}, stored)


@pytest.mark.parametrize(
    "incoming",
    [
        {"matching": {"tolerance_pct": "9"}},
        {"matching": {}},
        {"matching": None},
    ],
)
def test_guard_refuses_a_change_and_names_the_router(incoming):
    with pytest.raises(HTTPException) as exc:
        guard_matching_block(incoming, {"matching": {"tolerance_pct": "2"}})
    assert exc.value.status_code == 422
    assert RULES in exc.value.detail


# ---------- endpoints (real database) ----------------------------------------


async def _settings(realdb, key: str = "a") -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
    return dict(org.settings or {})


async def _seed_settings(realdb, extra: dict, key: str = "a") -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), **extra}
        flag_modified(org, "settings")
        await s.commit()


async def _vendor(realdb, name: str = "Acme Supplies", key: str = "a") -> uuid.UUID:
    async with realdb.sessionmaker(key)() as s:
        v = Vendor(name=name, organization_id=realdb.info(key).org_id)
        s.add(v)
        await s.commit()
        return v.id


async def _gl(realdb, code: str = "6100", key: str = "a") -> str:
    async with realdb.sessionmaker(key)() as s:
        s.add(
            GLAccount(
                organization_id=realdb.info(key).org_id,
                code=code,
                name="Freight",
                account_type="expense",
            )
        )
        await s.commit()
    return code


async def _rows(realdb, action: str, key: str = "a") -> list[AuditLog]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
async def test_admin_only(realdb, role):
    vendor_id = await _vendor(realdb)
    async with realdb.client(key="a", role=role) as c:
        assert (await c.get(RULES)).status_code == 403
        put = await c.put(f"{RULES}/vendors/{vendor_id}", json={"tolerance_pct": "1"})
        assert put.status_code == 403
        assert (await c.delete(f"{RULES}/vendors/{vendor_id}")).status_code == 403
        assert (
            await c.put(f"{RULES}/organization", json={"tolerance_pct": "9"})
        ).status_code == 403
    assert "matching" not in await _settings(realdb)


@pytest.mark.asyncio
async def test_defaults_read_back_with_no_block(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(RULES)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "organization": {
            "tolerance_pct": None,
            "require_inspection": None,
            "tolerance_out_of_range": False,
        },
        "built_in": {"tolerance_pct": "5", "require_inspection": False},
        "vendor_rules": [],
        "commodity_rules": [],
    }


@pytest.mark.asyncio
async def test_vendor_rule_create_edit_delete_audits_and_reaches_the_resolver(realdb):
    vendor_id = await _vendor(realdb)
    url = f"{RULES}/vendors/{vendor_id}"
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(url, json={"tolerance_pct": "1.5"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["vendor_rules"] == [
            {
                "vendor_id": str(vendor_id),
                "vendor_name": "Acme Supplies",
                "tolerance_pct": "1.5",
                "require_inspection": None,
                "tolerance_out_of_range": False,
            }
        ]
        stored = await _settings(realdb)
        rule = resolve_match_rule(stored, vendor_id=vendor_id, gl_account=None)
        assert rule.tolerance_pct == Decimal("1.5")

        # Same body again: nothing to record.
        assert (await c.put(url, json={"tolerance_pct": "1.5"})).status_code == 200
        edit = await c.put(url, json={"tolerance_pct": "0.5", "require_inspection": True})
        assert edit.status_code == 200, edit.text
        stored = await _settings(realdb)
        rule = resolve_match_rule(stored, vendor_id=vendor_id, gl_account=None)
        assert (rule.tolerance_pct, rule.require_inspection) == (Decimal("0.5"), True)

        gone = await c.delete(url)
        assert gone.status_code == 200, gone.text
        assert gone.json()["vendor_rules"] == []
        # Removing an absent rule is a no-op.
        assert (await c.delete(url)).status_code == 200

    updated = await _rows(realdb, "organization.match_rule_updated")
    assert [r.details["new"] for r in updated] == [
        {"tolerance_pct": "1.5", "require_inspection": None},
        {"tolerance_pct": "0.5", "require_inspection": True},
    ]
    assert updated[0].details["old"] is None
    assert updated[0].details["scope"] == "vendor"
    removed = await _rows(realdb, "organization.match_rule_removed")
    assert len(removed) == 1
    assert removed[0].details["old"] == {"tolerance_pct": "0.5", "require_inspection": True}
    assert removed[0].details["new"] is None


@pytest.mark.asyncio
async def test_commodity_rule_keeps_siblings_and_needs_a_chart_code(realdb):
    await _seed_settings(realdb, {"matching": {"tolerance_pct": "4", "vendor_rules": {}}})
    code = await _gl(realdb)
    async with realdb.client(key="a", role="admin") as c:
        missing = await c.put(f"{RULES}/commodities/9999", json={"tolerance_pct": "1"})
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "match_rule_gl_account_not_found"
        resp = await c.put(f"{RULES}/commodities/{code}", json={"require_inspection": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
    assert body["organization"] == {
        "tolerance_pct": "4",
        "require_inspection": None,
        "tolerance_out_of_range": False,
    }
    assert body["commodity_rules"] == [
        {
            "gl_account": code,
            "gl_account_name": "Freight",
            "tolerance_pct": None,
            "require_inspection": True,
            "tolerance_out_of_range": False,
        }
    ]
    assert (await _settings(realdb))["matching"] == {
        "tolerance_pct": "4",
        "vendor_rules": {},
        "commodity_rules": {code: {"require_inspection": True}},
    }


@pytest.mark.asyncio
async def test_refusals_are_coded(realdb):
    vendor_id = await _vendor(realdb)
    async with realdb.client(key="a", role="admin") as c:
        empty = await c.put(f"{RULES}/vendors/{vendor_id}", json={})
        assert empty.status_code == 422
        assert empty.json()["detail"]["code"] == "match_rule_empty"
        unknown = await c.put(f"{RULES}/vendors/{uuid.uuid4()}", json={"tolerance_pct": "1"})
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "match_rule_vendor_not_found"
        wide = await c.put(f"{RULES}/vendors/{vendor_id}", json={"tolerance_pct": "150"})
        assert wide.status_code == 422
        assert wide.json()["detail"] == {
            "code": "match_tolerance_out_of_range",
            "message": "The tolerance must be between 0 and 100 percent.",
            "params": {"min": "0", "max": "100"},
        }
        extra = await c.put(f"{RULES}/vendors/{vendor_id}", json={"tolerance_pct": "1", "x": 1})
        assert extra.status_code == 422
    assert "matching" not in await _settings(realdb)


@pytest.mark.asyncio
async def test_organization_layer_round_trip(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(
            f"{RULES}/organization", json={"tolerance_pct": "3", "require_inspection": False}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["organization"] == {
            "tolerance_pct": "3",
            "require_inspection": False,
            "tolerance_out_of_range": False,
        }
        back = await c.put(f"{RULES}/organization", json={})
        assert back.json()["organization"] == {
            "tolerance_pct": None,
            "require_inspection": None,
            "tolerance_out_of_range": False,
        }
    rows = await _rows(realdb, "organization.match_rule_updated")
    assert [r.details["scope"] for r in rows] == ["organization", "organization"]


@pytest.mark.asyncio
async def test_unwritable_audit_row_saves_nothing(realdb, monkeypatch):
    import app.api.organization_match_rules as mod

    async def boom(**_kwargs):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(mod, "record_auth_audit_or_raise", boom)
    vendor_id = await _vendor(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{RULES}/vendors/{vendor_id}", json={"tolerance_pct": "1"})
    assert resp.status_code == 503
    assert "matching" not in await _settings(realdb)


@pytest.mark.asyncio
async def test_generic_patch_cannot_change_the_block(realdb):
    await _seed_settings(realdb, {"matching": {"tolerance_pct": "2"}})
    async with realdb.client(key="a", role="admin") as c:
        refused = await c.patch(
            "/api/organization", json={"settings": {"matching": {"tolerance_pct": "9"}}}
        )
        assert refused.status_code == 422
        same = await c.patch(
            "/api/organization", json={"settings": {"matching": {"tolerance_pct": "2"}}}
        )
        assert same.status_code == 200, same.text
    assert (await _settings(realdb))["matching"] == {"tolerance_pct": "2"}


@pytest.mark.parametrize(
    ("value", "flagged"),
    [
        (None, False),
        (Decimal("0"), False),
        (Decimal("100"), False),
        (Decimal("-5"), True),
        (Decimal("500"), True),
        (Decimal("100.01"), True),
    ],
)
def test_tolerance_out_of_range_matches_the_writer_bounds(value, flagged):
    assert tolerance_out_of_range(value) is flagged


@pytest.mark.asyncio
async def test_a_stored_out_of_range_tolerance_is_reported_not_rewritten(realdb):
    """Old unaudited PATCHes could store values this writer refuses. The
    resolver still applies them, so the read shows them as stored and flags
    them — it neither hides them as "not set" nor rewrites them."""
    vendor_id = await _vendor(realdb)
    block = {
        "tolerance_pct": "500",
        "vendor_rules": {str(vendor_id): {"tolerance_pct": "-5"}},
    }
    await _seed_settings(realdb, {"matching": block})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(RULES)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization"] == {
        "tolerance_pct": "500",
        "require_inspection": None,
        "tolerance_out_of_range": True,
    }
    assert body["vendor_rules"][0]["tolerance_pct"] == "-5"
    assert body["vendor_rules"][0]["tolerance_out_of_range"] is True
    # Reading changed nothing.
    assert (await _settings(realdb))["matching"] == block
