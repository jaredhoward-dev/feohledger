"""Partner / reseller link provisioning — attach + detach (`/api/partner`).

Covers the two-sided-consent provisioning flow that LETS a partner create a
parent/child link without a raw DB statement, and the privilege boundary that
makes it safe:

  * Mint a link code (admin-only, fail-closed without a key).
  * Attach: a partner redeems a CHILD-issued code → the link is created, audited
    on BOTH org trails (PII-free).
  * **The authorization headline** — a partner CANNOT attach an org that never
    consented: with no code (or a forged / cross-key one) the attach is rejected
    and NO link is created. This is the cross-tenant takeover the model prevents.
  * Single-use: a redeemed code can't be replayed (409).
  * Re-parent guard: a child already linked to a partner can't be silently taken
    over (409); re-linking to the SAME partner is the idempotent no-op.
  * Detach: admin-only, scoped to the caller's own children (opaque 404 for a
    non-child), sets parent_org_id NULL, audited on both trails.
  * RBAC: a non-admin is 403 on every mutation.

Isolation note (mirrors `test_partner_admin.py`): the `realdb` control
Organization rows persist across the session, so each test resets the
parent/child link in a `finally` — order-independent.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select, text, update

from app.api import partner as partner_api
from app.config import settings
from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services import partner_capability
from app.services.partner_link_token import build_link_code
from tests.partner_capability_helpers import granted_partner


@pytest.fixture(autouse=True)
async def _a_is_partner(realdb):
    """Tenant "a" is the partner in this module: the operator granted it the
    capability every `/api/partner` route requires (decisions §574)."""
    async with granted_partner(realdb, "a"):
        yield


_KEY = "integration-partner-link-key"


@pytest.fixture
def partner_key(monkeypatch):
    """Configure a signing key + a short TTL so codes can be minted/redeemed."""
    monkeypatch.setattr(settings, "partner_link_signing_key", _KEY)
    monkeypatch.setattr(settings, "partner_link_ttl_minutes", 30)


async def _set_parent(realdb, *, child_key: str, parent_key: str | None) -> None:
    child_id = realdb.info(child_key).org_id
    parent_id = realdb.info(parent_key).org_id if parent_key is not None else None
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            update(Organization).where(Organization.id == child_id).values(parent_org_id=parent_id)
        )
        await s.commit()


async def _parent_of(realdb, child_key: str):
    child_id = realdb.info(child_key).org_id
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization.parent_org_id).where(Organization.id == child_id))
        ).scalar_one()


async def _audit_rows(realdb, tenant_key: str, action: str) -> list:
    """All AuditLog rows for ``action`` on tenant ``tenant_key``'s trail."""
    async with realdb.sessionmaker(tenant_key)() as s:
        return (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_link_code_admin_only(realdb, partner_key):
    async with realdb.client(key="b", role="admin") as c:
        ok = await c.post("/api/organization/partner-link-code")
    assert ok.status_code == 200
    body = ok.json()
    assert body["link_code"]
    assert body["expires_in_minutes"] == 30

    async with realdb.client(key="b", role="ap_manager") as c:
        denied = await c.post("/api/organization/partner-link-code")
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_mint_link_code_requires_auth(realdb, partner_key):
    async with realdb.client(key="b", role=None) as c:
        resp = await c.post("/api/organization/partner-link-code")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mint_link_code_fails_closed_without_key(realdb, monkeypatch):
    monkeypatch.setattr(settings, "partner_link_signing_key", "")
    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post("/api/organization/partner-link-code")
    assert resp.status_code == 503
    # Coded, so the Organization settings card states it in the admin's locale
    # (the client catalogue registers `partner_linking_disabled`).
    assert resp.json()["detail"]["code"] == "partner_linking_disabled"
    assert resp.json()["detail"]["message"] == "Partner linking is not enabled."


@pytest.mark.asyncio
async def test_mint_link_code_needs_no_partner_capability_and_is_audited(realdb, partner_key):
    """Decisions §574: consenting to BE adopted is not a partner act. B was never
    granted the capability, its admin still mints from Organization settings,
    and the issue is audited on B's own trail as before."""
    before = len(await _audit_rows(realdb, "b", "partner.link_code_issued"))
    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post("/api/organization/partner-link-code")
    assert resp.status_code == 200
    rows = await _audit_rows(realdb, "b", "partner.link_code_issued")
    assert len(rows) == before + 1
    assert any(r.details == {"ttl_minutes": 30} for r in rows)


@pytest.mark.asyncio
async def test_old_partner_link_code_route_is_gone(realdb, partner_key):
    """Deliberate: the mint moved to ``POST /api/organization/partner-link-code``
    (decisions §574) and the old path must not survive as a second door under
    different rules. No ``/api/partner`` route matches it in any method, so it is
    a plain 404 — a 403 here would mean the old route is back behind the
    capability gate, a 200 that it is back ungated."""
    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post("/api/partner/link-code")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_partner_cannot_redeem_a_code(realdb, partner_key):
    """A's admin consents (mints); B — not a partner — cannot adopt A with it."""
    async with realdb.client(key="a", role="admin") as c:
        code = (await c.post("/api/organization/partner-link-code")).json()["link_code"]
    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post("/api/partner/children", json={"link_code": code})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "partner_capability_required"
    assert await _parent_of(realdb, "a") is None


# ---------------------------------------------------------------------------
# Attach — happy path + audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_with_consenting_code_links_and_audits(realdb, partner_key):
    """B's admin mints a code; A redeems it → B becomes A's child, both audited."""
    await _set_parent(realdb, child_key="b", parent_key=None)
    try:
        # Child B consents by minting a code.
        async with realdb.client(key="b", role="admin") as c:
            mint = await c.post("/api/organization/partner-link-code")
        code = mint.json()["link_code"]

        # Partner A redeems it.
        async with realdb.client(key="a", role="admin") as c:
            attach = await c.post("/api/partner/children", json={"link_code": code})
        assert attach.status_code == 201
        assert attach.json()["slug"] == realdb.info("b").slug

        # Link persisted: B.parent_org_id == A.
        assert await _parent_of(realdb, "b") == realdb.info("a").org_id

        # Audited on the PARTNER's trail (a) — child_attached, PII-free.
        a_rows = await _audit_rows(realdb, "a", "partner.child_attached")
        assert a_rows
        assert a_rows[-1].details.get("child_org_id") == str(realdb.info("b").org_id)
        assert realdb.info("b").slug not in str(a_rows[-1].details)

        # Audited on the CHILD's trail (b) — parent_linked, PII-free.
        b_rows = await _audit_rows(realdb, "b", "partner.parent_linked")
        assert b_rows
        assert b_rows[-1].details.get("partner_org_id") == str(realdb.info("a").org_id)
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


# ---------------------------------------------------------------------------
# Attach — the authorization headline: no unilateral adoption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_without_consent_is_rejected(realdb, partner_key):
    """A partner cannot adopt an org that never minted a code (no valid code)."""
    await _set_parent(realdb, child_key="b", parent_key=None)
    try:
        async with realdb.client(key="a", role="admin") as c:
            # Garbage code — no child consented.
            resp = await c.post("/api/partner/children", json={"link_code": "totally.bogus"})
        assert resp.status_code == 400
        # No link created — B is still standalone.
        assert await _parent_of(realdb, "b") is None
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


@pytest.mark.asyncio
async def test_attach_with_forged_cross_key_code_is_rejected(realdb, partner_key):
    """A code signed with a DIFFERENT key (a forged/leaked-other-env one)
    targeting B must not let A adopt B — the signature is the consent proof."""
    await _set_parent(realdb, child_key="b", parent_key=None)
    try:
        forged = build_link_code(
            child_org_id=realdb.info("b").org_id,
            signing_key="some-other-key-not-ours",
            ttl_minutes=30,
        )
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post("/api/partner/children", json={"link_code": forged})
        assert resp.status_code == 400
        assert await _parent_of(realdb, "b") is None
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


@pytest.mark.asyncio
async def test_attach_admin_only(realdb, partner_key):
    async with realdb.client(key="b", role="admin") as c:
        code = (await c.post("/api/organization/partner-link-code")).json()["link_code"]
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/partner/children", json={"link_code": code})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Attach — single-use + re-parent guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_code_is_single_use(realdb, partner_key):
    """A redeemed code can't be replayed (e.g. after a detach) — 409."""
    await _set_parent(realdb, child_key="b", parent_key=None)
    try:
        async with realdb.client(key="b", role="admin") as c:
            code = (await c.post("/api/organization/partner-link-code")).json()["link_code"]
        async with realdb.client(key="a", role="admin") as c:
            first = await c.post("/api/partner/children", json={"link_code": code})
            # Detach so the re-parent guard isn't what trips the replay.
            await c.delete(f"/api/partner/children/{realdb.info('b').org_id}")
            replay = await c.post("/api/partner/children", json={"link_code": code})
        assert first.status_code == 201
        assert replay.status_code == 409
        # The replay did NOT re-link.
        assert await _parent_of(realdb, "b") is None
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


@pytest.mark.asyncio
async def test_reparent_guard_blocks_takeover_but_same_partner_is_idempotent(realdb, partner_key):
    """A child already linked to A can't be silently taken over; re-linking to
    the SAME partner is the idempotent no-op."""
    # Pre-link B to A out of band.
    await _set_parent(realdb, child_key="b", parent_key="a")
    try:
        # Same-partner re-attach: idempotent 201 (returns the summary), no error.
        async with realdb.client(key="b", role="admin") as c:
            code = (await c.post("/api/organization/partner-link-code")).json()["link_code"]
        async with realdb.client(key="a", role="admin") as c:
            same = await c.post("/api/partner/children", json={"link_code": code})
        assert same.status_code == 201
        assert await _parent_of(realdb, "b") == realdb.info("a").org_id
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


# ---------------------------------------------------------------------------
# Detach
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detach_unlinks_and_audits(realdb, partner_key):
    await _set_parent(realdb, child_key="b", parent_key="a")
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.delete(f"/api/partner/children/{realdb.info('b').org_id}")
        assert resp.status_code == 204
        assert await _parent_of(realdb, "b") is None

        assert await _audit_rows(realdb, "a", "partner.child_detached")
        assert await _audit_rows(realdb, "b", "partner.parent_unlinked")
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


@pytest.mark.asyncio
async def test_detach_non_child_is_opaque_404(realdb, partner_key):
    """ISOLATION: A can't detach an org it doesn't parent — opaque 404, no effect."""
    await _set_parent(realdb, child_key="b", parent_key=None)  # B standalone
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.delete(f"/api/partner/children/{realdb.info('b').org_id}")
    assert resp.status_code == 404
    assert await _parent_of(realdb, "b") is None


@pytest.mark.asyncio
async def test_detach_admin_only(realdb, partner_key):
    await _set_parent(realdb, child_key="b", parent_key="a")
    try:
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.delete(f"/api/partner/children/{realdb.info('b').org_id}")
        assert resp.status_code == 403
        # Still linked — the denied call changed nothing.
        assert await _parent_of(realdb, "b") == realdb.info("a").org_id
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


# ---------------------------------------------------------------------------
# Attach vs operator revoke — serialized on the partner's row (decisions §578)
# ---------------------------------------------------------------------------


async def _wait_for_control_lock_waiter(realdb, *, timeout: float = 15.0) -> bool:
    """True once Postgres reports another backend in the control-plane database
    waiting on a lock — the server's own wait state, never a slept guess."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with realdb.control_sessionmaker()() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.05)
    return False


async def _mint(realdb, key: str) -> str:
    async with realdb.client(key=key, role="admin") as c:
        return (await c.post("/api/organization/partner-link-code")).json()["link_code"]


async def _attach(realdb, code: str):
    async with realdb.client(key="a", role="admin") as c:
        return await c.post("/api/partner/children", json={"link_code": code})


@pytest.mark.asyncio
async def test_revoke_holding_the_partner_lock_makes_a_queued_attach_refuse(realdb, partner_key):
    """The revoke counts zero children under A's row lock; an attach arriving
    then must wait for that lock and, once the revoke commits, refuse — never
    leave B parented by a workspace without the capability."""
    await _set_parent(realdb, child_key="b", parent_key=None)
    code = await _mint(realdb, "b")
    lock_held, release = asyncio.Event(), asyncio.Event()
    real_audit = partner_capability.record_auth_audit_or_raise

    async def _gated_audit(**kwargs):
        lock_held.set()  # the count is done and A's row is locked
        await release.wait()
        return await real_audit(**kwargs)

    async def _revoke():
        async with realdb.control_sessionmaker()() as s:
            return await partner_capability.set_partner_capability(
                s, realdb.info("a").slug, enabled=False
            )

    try:
        with patch.object(partner_capability, "record_auth_audit_or_raise", new=_gated_audit):
            revoke_task = asyncio.create_task(_revoke())
            await asyncio.wait_for(lock_held.wait(), timeout=15)
            attach_task = asyncio.create_task(_attach(realdb, code))
            queued = await _wait_for_control_lock_waiter(realdb)
            release.set()
            revoked, attach = await asyncio.gather(revoke_task, attach_task)
        assert queued, "the attach never queued behind the revoke's lock on A's row"
        assert revoked.changed and revoked.is_partner is False
        assert attach.status_code == 403
        assert attach.json()["detail"]["code"] == "partner_capability_required"
        assert await _parent_of(realdb, "b") is None
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)


@pytest.mark.asyncio
async def test_attach_holding_the_partner_lock_makes_a_queued_revoke_refuse(realdb, partner_key):
    """The other order: an attach holding A's row lock links B; the revoke
    waits for it, then counts the new child and refuses."""
    await _set_parent(realdb, child_key="b", parent_key=None)
    code = await _mint(realdb, "b")
    lock_held, release = asyncio.Event(), asyncio.Event()
    real_lock = partner_api._lock_partner_and_child

    async def _gated_lock(*args, **kwargs):
        rows = await real_lock(*args, **kwargs)
        lock_held.set()
        await release.wait()
        return rows

    async def _revoke():
        async with realdb.control_sessionmaker()() as s:
            try:
                return await partner_capability.set_partner_capability(
                    s, realdb.info("a").slug, enabled=False
                )
            except partner_capability.PartnerHasChildren as exc:
                return exc

    try:
        with patch.object(partner_api, "_lock_partner_and_child", new=_gated_lock):
            attach_task = asyncio.create_task(_attach(realdb, code))
            await asyncio.wait_for(lock_held.wait(), timeout=15)
            revoke_task = asyncio.create_task(_revoke())
            queued = await _wait_for_control_lock_waiter(realdb)
            release.set()
            attach, revoked = await asyncio.gather(attach_task, revoke_task)
        assert queued, "the revoke never queued behind the attach's lock on A's row"
        assert attach.status_code == 201
        assert isinstance(revoked, partner_capability.PartnerHasChildren)
        assert revoked.count == 1
        assert await _parent_of(realdb, "b") == realdb.info("a").org_id
    finally:
        await _set_parent(realdb, child_key="b", parent_key=None)
