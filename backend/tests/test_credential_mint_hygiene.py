"""Credential mints in the admin surface, held to the platform's own rules.

(Admin-created users used to get a generated temporary password, pinned here to
the complexity policy. No flow mints one any more — invites send a set-password
link, `tests/test_admin_create_user_email.py`, decisions §283.)

* **Minting a SCIM bearer token is audited.** That token is a tenant-wide
  user-provisioning credential — whoever holds it can create, rename and
  deactivate accounts in the org, and grant roles through group mapping. Every
  other credential mint on the platform writes an append-only row
  (`api_key.created`, `webhook_subscription.created`); this one wrote nothing,
  so a rotation left no trace for an access review. The row is PII-free and
  carries only the non-secret digest prefix.
* **Audit first, for mint and revoke** (decisions §510): a row that cannot be
  written is a 503 with the live token untouched. Revoke clears both copies of
  the digest, is audited with the removed prefix, and is not plan-gated.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.models.organization import Organization
from app.models.workflow import AuditLog

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def scim_hash_restored(realdb):
    """Put `organizations.scim_bearer_hash` back after the test.

    It is a dedicated column, not part of the `settings` JSONB the harness
    baselines between tests, so a mint here would otherwise persist for the life
    of the per-slot control database.
    """
    mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    async with mk() as s:
        before = (
            await s.execute(select(Organization.scim_bearer_hash).where(Organization.id == org_id))
        ).scalar_one()
    yield
    async with mk() as s:
        await s.execute(
            update(Organization).where(Organization.id == org_id).values(scim_bearer_hash=before)
        )
        await s.commit()


@pytest.mark.plan("scale")  # minting a SCIM token is a Scale feature (decisions §258)
async def test_minting_a_scim_token_writes_a_secret_free_audit_row(realdb, scim_hash_restored):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/organization/sso/scim-token")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["token"]
    prefix = body["bearer_hash_prefix"]

    tmk = realdb.sessionmaker("a")
    async with tmk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "organization.scim_token_minted")
                )
            )
            .scalars()
            .all()
        )
    assert rows, "minting a SCIM bearer token must be audited"
    details = rows[-1].details
    assert details["bearer_hash_prefix"] == prefix
    # The trail records WHICH token is live, never the token or its full digest.
    assert token not in str(details)

    # And the mint really did rotate the indexed column the SCIM auth resolves on.
    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
    assert org.scim_bearer_hash is not None
    assert org.scim_bearer_hash.startswith(prefix)
    assert details["rotated"] in (True, False)


async def _audit_rows(realdb, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
        )


async def _org(realdb) -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()


@pytest.mark.plan("scale")
async def test_a_second_mint_is_recorded_as_a_rotation(realdb, scim_hash_restored):
    async with realdb.client(key="a", role="admin") as c:
        first = (await c.post("/api/organization/sso/scim-token")).json()
        second = (await c.post("/api/organization/sso/scim-token")).json()
    assert first["token"] != second["token"]
    rows = await _audit_rows(realdb, "organization.scim_token_minted")
    assert rows[-1].details == {"bearer_hash_prefix": second["bearer_hash_prefix"], "rotated": True}


@pytest.mark.plan("scale")
async def test_no_audit_row_means_no_mint(realdb, monkeypatch, scim_hash_restored):
    from app.services import audit_dispatch

    before = (await _org(realdb)).scim_bearer_hash

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/organization/sso/scim-token")
    assert resp.status_code == 503
    assert "token" not in resp.text
    assert (await _org(realdb)).scim_bearer_hash == before


@pytest.mark.plan("scale")
async def test_revoke_clears_both_copies_and_is_audited(realdb, scim_hash_restored):
    async with realdb.client(key="a", role="admin") as c:
        minted = (await c.post("/api/organization/sso/scim-token")).json()
        assert (await c.get("/api/organization/sso")).json()["scim_token_configured"] is True
        resp = await c.delete("/api/organization/sso/scim-token")
        assert resp.status_code == 204, resp.text
        assert (await c.get("/api/organization/sso")).json()["scim_token_configured"] is False
        # The revoked token no longer authenticates a SCIM call.
        scim = await c.get(
            "/api/scim/v2/Users", headers={"Authorization": f"Bearer {minted['token']}"}
        )
        assert scim.status_code == 401

    org = await _org(realdb)
    assert org.scim_bearer_hash is None
    assert "scim_bearer_hash" not in ((org.settings or {}).get("sso") or {})
    rows = await _audit_rows(realdb, "organization.scim_token_revoked")
    assert rows[-1].details == {"bearer_hash_prefix": minted["bearer_hash_prefix"]}
    assert minted["token"] not in str(rows[-1].details)


@pytest.mark.plan("scale")
async def test_revoke_with_no_token_is_a_silent_no_op(realdb, scim_hash_restored):
    async with realdb.client(key="a", role="admin") as c:
        await c.delete("/api/organization/sso/scim-token")  # clear whatever is seeded
        before = len(await _audit_rows(realdb, "organization.scim_token_revoked"))
        assert (await c.delete("/api/organization/sso/scim-token")).status_code == 204
    assert len(await _audit_rows(realdb, "organization.scim_token_revoked")) == before


@pytest.mark.plan("scale")
async def test_no_audit_row_means_no_revoke(realdb, monkeypatch, scim_hash_restored):
    from app.services import audit_dispatch

    async with realdb.client(key="a", role="admin") as c:
        assert (await c.post("/api/organization/sso/scim-token")).status_code == 200
    live = (await _org(realdb)).scim_bearer_hash

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.delete("/api/organization/sso/scim-token")).status_code == 503
    assert (await _org(realdb)).scim_bearer_hash == live


@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
async def test_only_an_admin_mints_or_revokes(realdb, role):
    async with realdb.client(key="a", role=role) as c:
        assert (await c.post("/api/organization/sso/scim-token")).status_code == 403
        assert (await c.delete("/api/organization/sso/scim-token")).status_code == 403


async def test_revoke_is_not_plan_gated(realdb, scim_hash_restored):
    """A tenant whose plan dropped SCIM can still take its token out of use."""
    await realdb.subscribe("a", "growth")
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        org.scim_bearer_hash = "ab" * 32
        await s.commit()
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.delete("/api/organization/sso/scim-token")).status_code == 204
    assert (await _org(realdb)).scim_bearer_hash is None
