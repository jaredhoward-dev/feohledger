"""The operator-granted partner capability (decisions §574).

``Organization.is_partner`` is what every ``/api/partner`` route requires. These
pin the parts outside that router: ``/api/auth/me`` reports it, the one writer
(``services/partner_capability`` via ``scripts/set_partner_capability.py``)
audits before it changes anything, is idempotent, and refuses to strand a
partner's children by revoking while any are linked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services import audit_dispatch
from app.services.partner_capability import (
    ACTION_GRANTED,
    ACTION_REVOKED,
    AuditWriteFailed,
    PartnerHasChildren,
    TenantNotFound,
    set_partner_capability,
)
from tests.partner_capability_helpers import granted_partner, set_is_partner


async def _is_partner(realdb, key: str) -> bool:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(
                select(Organization.is_partner).where(Organization.id == realdb.info(key).org_id)
            )
        ).scalar_one()


async def _audit_rows(realdb, key: str, action: str) -> list:
    async with realdb.sessionmaker(key)() as s:
        return (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()


async def _set_parent(realdb, child_key: str, parent_key: str | None) -> None:
    parent_id = realdb.info(parent_key).org_id if parent_key else None
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == realdb.info(child_key).org_id)
            .values(parent_org_id=parent_id)
        )
        await s.commit()


@pytest.mark.asyncio
async def test_auth_me_reports_the_capability(realdb):
    async with realdb.client(key="b", role="admin") as c:
        assert (await c.get("/api/auth/me")).json()["is_partner"] is False
    async with granted_partner(realdb, "b"):
        async with realdb.client(key="b", role="ap_clerk") as c:
            # A property of the workspace, reported to every role; the routes
            # still require admin on top of it.
            assert (await c.get("/api/auth/me")).json()["is_partner"] is True


@pytest.mark.asyncio
async def test_grant_and_revoke_are_audited_and_idempotent(realdb):
    slug = realdb.info("b").slug
    granted_before = len(await _audit_rows(realdb, "b", ACTION_GRANTED))
    revoked_before = len(await _audit_rows(realdb, "b", ACTION_REVOKED))
    try:
        async with realdb.control_sessionmaker()() as s:
            result = await set_partner_capability(s, slug, enabled=True, reason=" CTR-1 ")
        assert result.changed and result.is_partner
        assert await _is_partner(realdb, "b") is True
        rows = await _audit_rows(realdb, "b", ACTION_GRANTED)
        assert len(rows) == granted_before + 1
        assert any(r.details == {"source": "operator", "reason": "CTR-1"} for r in rows)

        async with realdb.control_sessionmaker()() as s:
            again = await set_partner_capability(s, slug, enabled=True)
        assert not again.changed
        assert len(await _audit_rows(realdb, "b", ACTION_GRANTED)) == granted_before + 1

        async with realdb.control_sessionmaker()() as s:
            revoked = await set_partner_capability(s, slug, enabled=False)
        assert revoked.changed and not revoked.is_partner
        assert await _is_partner(realdb, "b") is False
        assert len(await _audit_rows(realdb, "b", ACTION_REVOKED)) == revoked_before + 1
    finally:
        await set_is_partner(realdb, "b", False)


@pytest.mark.asyncio
async def test_revoke_is_refused_while_children_are_linked(realdb):
    await _set_parent(realdb, "b", "a")
    try:
        async with granted_partner(realdb, "a"):
            async with realdb.control_sessionmaker()() as s:
                with pytest.raises(PartnerHasChildren) as exc:
                    await set_partner_capability(s, realdb.info("a").slug, enabled=False)
            assert exc.value.count == 1
            assert await _is_partner(realdb, "a") is True
    finally:
        await _set_parent(realdb, "b", None)


@pytest.mark.asyncio
async def test_unknown_slug(realdb):
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(TenantNotFound):
            await set_partner_capability(s, "no-such-tenant-zz", enabled=True)


@pytest.mark.asyncio
async def test_no_audit_row_means_no_change(realdb, monkeypatch):
    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    async with realdb.control_sessionmaker()() as s:
        with pytest.raises(AuditWriteFailed):
            await set_partner_capability(s, realdb.info("b").slug, enabled=True)
    assert await _is_partner(realdb, "b") is False


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "set_partner_capability.py"
    spec = importlib.util.spec_from_file_location("set_partner_capability_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_script_exit_codes(realdb, capsys):
    script = _load_script()
    parser = script.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--slug", "x"])  # --grant or --revoke is required
    with pytest.raises(SystemExit):
        parser.parse_args(["--slug", "x", "--grant", "--revoke"])

    slug = realdb.info("b").slug
    try:
        assert await script.run(slug, enabled=True, reason="T-1") == 0
        assert "now a partner" in capsys.readouterr().out
        assert await script.run(slug, enabled=True, reason=None) == 0
        assert "Nothing changed" in capsys.readouterr().out
        assert await script.run("no-such-tenant-zz", enabled=True, reason=None) == 1

        await _set_parent(realdb, "a", "b")
        try:
            assert await script.run(slug, enabled=False, reason=None) == 3
            assert "still parents 1" in capsys.readouterr().err
        finally:
            await _set_parent(realdb, "a", None)
        assert await script.run(slug, enabled=False, reason=None) == 0
    finally:
        await set_is_partner(realdb, "b", False)
