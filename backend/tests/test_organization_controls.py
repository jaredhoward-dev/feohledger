"""`api/organization_controls` — the audited writers of two single keys
(decisions §502): `settings.fraud_rules.line_total_mismatch_enabled` and
`settings.exception_agents.autonomy_level`.

Covers:

  * RBAC — admin-only on GET and PUT; every other role is 403.
  * Validation — only a real bool / a known level is accepted.
  * The write carries the block's siblings, reads back the effective value,
    and records `old` / `new` in the audit row; a no-op save writes no row.
  * Audit first: a row that cannot be written is a 503 with nothing saved.
  * The generic `PATCH /api/organization` carries both keys across a block
    replacement and refuses a different value (`guard_owned_keys`).

`Organization.settings` is reset by the `realdb` harness before every test.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.api.organization_controls import guard_owned_keys, line_total_check_enabled
from app.models.organization import Organization
from app.models.workflow import AuditLog

LINE_TOTAL = "/api/organization/line-total-check"
AUTONOMY = "/api/organization/exception-agents/autonomy"


# ---------- pure helpers (no database) ---------------------------------------


def test_line_total_check_defaults_on():
    assert line_total_check_enabled(None) is True
    assert line_total_check_enabled({"fraud_rules": {}}) is True
    off = {"fraud_rules": {"line_total_mismatch_enabled": False}}
    assert line_total_check_enabled(off) is False


@pytest.mark.parametrize("stored", [0, None, "", 0.0, False])
def test_line_total_check_reads_any_falsy_value_off_as_the_engine_does(stored):
    """`invoice_warnings` skips the check on any falsy stored value, so the
    endpoint must report it off — never `enabled: true` over a disabled check."""
    from app.services.invoice_warnings import _fraud_config

    org_settings = {"fraud_rules": {"line_total_mismatch_enabled": stored}}
    assert bool(_fraud_config(org_settings)["line_total_mismatch_enabled"]) is False
    assert line_total_check_enabled(org_settings) is False


def test_guard_carries_stored_values_across_a_block_replacement():
    stored = {
        "fraud_rules": {"line_total_mismatch_enabled": False, "round_amount_enabled": True},
        "exception_agents": {"autonomy_level": "balanced", "amount_tolerance_pct": 2.5},
    }
    incoming = {
        "fraud_rules": {"round_amount_enabled": False},
        "exception_agents": {"amount_tolerance_pct": 3},
    }
    guard_owned_keys(incoming, stored)
    assert incoming["fraud_rules"] == {
        "round_amount_enabled": False,
        "line_total_mismatch_enabled": False,
    }
    assert incoming["exception_agents"] == {"amount_tolerance_pct": 3, "autonomy_level": "balanced"}


def test_guard_passes_an_unchanged_value_and_leaves_unset_keys_unset():
    incoming = {
        "fraud_rules": {"line_total_mismatch_enabled": True},
        "exception_agents": {"autonomy_level": "conservative"},
    }
    guard_owned_keys(incoming, {})
    assert incoming == {
        "fraud_rules": {"line_total_mismatch_enabled": True},
        "exception_agents": {"autonomy_level": "conservative"},
    }
    other = {"fraud_rules": {}}
    guard_owned_keys(other, {})
    assert other == {"fraud_rules": {}}


@pytest.mark.parametrize(
    ("incoming", "path"),
    [
        ({"fraud_rules": {"line_total_mismatch_enabled": False}}, LINE_TOTAL),
        ({"exception_agents": {"autonomy_level": "aggressive"}}, AUTONOMY),
    ],
)
def test_guard_refuses_a_change_and_names_the_owning_endpoint(incoming, path):
    with pytest.raises(HTTPException) as exc:
        guard_owned_keys(incoming, {})
    assert exc.value.status_code == 422
    assert path in exc.value.detail


@pytest.mark.parametrize("block", ["fraud_rules", "exception_agents"])
@pytest.mark.parametrize("value", [None, "x", [], 0])
def test_guard_refuses_a_non_object_block(block, value):
    """A present non-object block would replace the whole block, owned key
    included, unaudited — and a string crashes `_fraud_config` /
    `resolve_autonomy_level` on every later read."""
    stored = {
        "fraud_rules": {"line_total_mismatch_enabled": False},
        "exception_agents": {"autonomy_level": "balanced"},
    }
    with pytest.raises(HTTPException) as exc:
        guard_owned_keys({block: value}, stored)
    assert exc.value.status_code == 422
    assert "/api/organization/" in exc.value.detail


@pytest.mark.parametrize(
    ("incoming", "stored", "path"),
    [
        # `0 == False`, but 0 is not the bool the rule stores.
        (
            {"fraud_rules": {"line_total_mismatch_enabled": 0}},
            {"fraud_rules": {"line_total_mismatch_enabled": False}},
            LINE_TOTAL,
        ),
        ({"fraud_rules": {"line_total_mismatch_enabled": 1}}, {}, LINE_TOTAL),
        ({"fraud_rules": {"line_total_mismatch_enabled": None}}, {}, LINE_TOTAL),
        ({"fraud_rules": {"line_total_mismatch_enabled": "true"}}, {}, LINE_TOTAL),
        # An unknown level reads `conservative`, but is not one.
        (
            {"exception_agents": {"autonomy_level": "supervised"}},
            {"exception_agents": {"autonomy_level": "supervised"}},
            AUTONOMY,
        ),
        ({"exception_agents": {"autonomy_level": None}}, {}, AUTONOMY),
        ({"exception_agents": {"autonomy_level": ["balanced"]}}, {}, AUTONOMY),
    ],
)
def test_guard_refuses_a_non_canonical_value(incoming, stored, path):
    with pytest.raises(HTTPException) as exc:
        guard_owned_keys(incoming, stored)
    assert exc.value.status_code == 422
    assert path in exc.value.detail


# ---------- endpoints (real database) ----------------------------------------


async def _settings(realdb, key: str = "a") -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
    return dict(org.settings or {})


async def _seed(realdb, extra: dict, key: str = "a") -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), **extra}
        flag_modified(org, "settings")
        await s.commit()


async def _rows(realdb, action: str, key: str = "a") -> list[AuditLog]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
@pytest.mark.parametrize(
    ("url", "body"),
    [(LINE_TOTAL, {"enabled": False}), (AUTONOMY, {"autonomy_level": "balanced"})],
)
async def test_admin_only(realdb, role, url, body):
    async with realdb.client(key="a", role=role) as c:
        assert (await c.get(url)).status_code == 403
        assert (await c.put(url, json=body)).status_code == 403
    stored = await _settings(realdb)
    assert "line_total_mismatch_enabled" not in (stored.get("fraud_rules") or {})
    assert "autonomy_level" not in (stored.get("exception_agents") or {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "body"),
    [
        (LINE_TOTAL, {"enabled": "false"}),
        (LINE_TOTAL, {"enabled": 0}),
        (LINE_TOTAL, {}),
        (AUTONOMY, {"autonomy_level": "suggest"}),
        (AUTONOMY, {"autonomy_level": None}),
        (AUTONOMY, {"autonomy_level": "balanced", "threshold": 0.1}),
    ],
)
async def test_invalid_values_are_refused(realdb, url, body):
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.put(url, json=body)).status_code == 422


@pytest.mark.asyncio
async def test_line_total_check_round_trip_keeps_siblings_and_audits(realdb):
    await _seed(realdb, {"fraud_rules": {"round_amount_enabled": False}})
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get(LINE_TOTAL)).json() == {"enabled": True}
        resp = await c.put(LINE_TOTAL, json={"enabled": False})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"enabled": False}
        assert (await c.get(LINE_TOTAL)).json() == {"enabled": False}
        # Same value again: nothing to record.
        assert (await c.put(LINE_TOTAL, json={"enabled": False})).status_code == 200
    assert (await _settings(realdb))["fraud_rules"] == {
        "round_amount_enabled": False,
        "line_total_mismatch_enabled": False,
    }
    rows = await _rows(realdb, "organization.fraud_rule_updated")
    assert len(rows) == 1
    assert rows[0].details["setting"] == "fraud_rules.line_total_mismatch_enabled"
    assert rows[0].details["old"] is True
    assert rows[0].details["new"] is False


@pytest.mark.asyncio
async def test_autonomy_round_trip_keeps_siblings_and_audits(realdb):
    await _seed(realdb, {"exception_agents": {"amount_tolerance_pct": 2.5}})
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get(AUTONOMY)).json() == {"autonomy_level": "conservative"}
        resp = await c.put(AUTONOMY, json={"autonomy_level": "balanced"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"autonomy_level": "balanced"}
        assert (await c.get(AUTONOMY)).json() == {"autonomy_level": "balanced"}
    assert (await _settings(realdb))["exception_agents"] == {
        "amount_tolerance_pct": 2.5,
        "autonomy_level": "balanced",
    }
    rows = await _rows(realdb, "organization.exception_agent_autonomy_updated")
    assert len(rows) == 1
    assert rows[0].details == {
        "setting": "exception_agents.autonomy_level",
        "old": "conservative",
        "new": "balanced",
    }


@pytest.mark.asyncio
async def test_an_unknown_stored_level_reads_conservative(realdb):
    await _seed(realdb, {"exception_agents": {"autonomy_level": "supervised"}})
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get(AUTONOMY)).json() == {"autonomy_level": "conservative"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "body"),
    [(LINE_TOTAL, {"enabled": False}), (AUTONOMY, {"autonomy_level": "aggressive"})],
)
async def test_no_audit_row_means_no_save(realdb, monkeypatch, url, body):
    from app.services import audit_dispatch

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.put(url, json=body)).status_code == 503
    stored = await _settings(realdb)
    assert "line_total_mismatch_enabled" not in (stored.get("fraud_rules") or {})
    assert "autonomy_level" not in (stored.get("exception_agents") or {})


@pytest.mark.asyncio
async def test_generic_patch_carries_and_refuses(realdb):
    await _seed(
        realdb,
        {
            "fraud_rules": {"line_total_mismatch_enabled": False},
            "exception_agents": {"autonomy_level": "balanced"},
        },
    )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"fraud_rules": {"round_amount_enabled": False}}},
        )
        assert resp.status_code == 200, resp.text
        refused = await c.patch(
            "/api/organization",
            json={"settings": {"exception_agents": {"autonomy_level": "aggressive"}}},
        )
        assert refused.status_code == 422
        assert AUTONOMY in refused.json()["detail"]
    stored = await _settings(realdb)
    assert stored["fraud_rules"] == {
        "round_amount_enabled": False,
        "line_total_mismatch_enabled": False,
    }
    assert stored["exception_agents"] == {"autonomy_level": "balanced"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"settings": {"fraud_rules": None}},
        {"settings": {"exception_agents": None}},
        {"settings": {"fraud_rules": "x"}},
        {"settings": {"fraud_rules": {"line_total_mismatch_enabled": 0}}},
        {"settings": {"exception_agents": {"autonomy_level": "supervised"}}},
    ],
)
async def test_generic_patch_refuses_a_non_object_block_or_non_canonical_value(realdb, body):
    seeded = {
        "fraud_rules": {"line_total_mismatch_enabled": False},
        "exception_agents": {"autonomy_level": "balanced"},
    }
    await _seed(realdb, seeded)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json=body)
        assert resp.status_code == 422, resp.text
    stored = await _settings(realdb)
    assert stored["fraud_rules"] == seeded["fraud_rules"]
    assert stored["exception_agents"] == seeded["exception_agents"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_value", [0, None])
async def test_a_stored_falsy_value_reads_off_and_put_true_rewrites_it(realdb, stored_value):
    """A non-bool falsy value has the engine's check OFF: the endpoint says so,
    and turning it on writes `True` with an audit row instead of a no-op."""
    await _seed(realdb, {"fraud_rules": {"line_total_mismatch_enabled": stored_value}})
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get(LINE_TOTAL)).json() == {"enabled": False}
        resp = await c.put(LINE_TOTAL, json={"enabled": True})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"enabled": True}
    assert (await _settings(realdb))["fraud_rules"]["line_total_mismatch_enabled"] is True
    rows = await _rows(realdb, "organization.fraud_rule_updated")
    assert len(rows) == 1
    assert rows[0].details["old"] is False
    assert rows[0].details["new"] is True
    assert rows[0].details["stored"] == stored_value


@pytest.mark.asyncio
async def test_put_false_over_a_stored_zero_normalises_it_with_an_audit_row(realdb):
    """Same effective value, non-canonical storage: rewritten to the bool, and
    the row records what was stored."""
    await _seed(realdb, {"fraud_rules": {"line_total_mismatch_enabled": 0}})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(LINE_TOTAL, json={"enabled": False})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"enabled": False}
    assert (await _settings(realdb))["fraud_rules"]["line_total_mismatch_enabled"] is False
    rows = await _rows(realdb, "organization.fraud_rule_updated")
    assert len(rows) == 1
    assert rows[0].details == {
        "setting": "fraud_rules.line_total_mismatch_enabled",
        "old": False,
        "new": False,
        "stored": 0,
    }
