"""A supplier-portal user's Privacy Policy acknowledgement (`docs/decisions.md` §499).

Pins: an invited portal account (no password yet) cannot redeem its
set-password link without acknowledging the Privacy Policy version in force —
refused with a coded 422 BEFORE the token is spent — and the redemption that
carries it records one tenant row in the same transaction; suppliers are never
asked for the Terms (a Terms version alone does not pass, and none is
recorded); a repeat keeps one row and the first ``accepted_at``; a
forgot-password reset of an account that already has a password is not held
up; the vendor-user DSAR export carries the rows and a DSAR erasure keeps them;
a workspace closure classifies the table.

The signed-in re-prompt (§500): ``/me`` (and every route answering with the
same profile) reports ``legal_acceptance_required`` while the version in force
is unacknowledged; ``POST /portal/auth/legal-acknowledgement`` records it for
the vendor user the portal JWT names — never one named in the body — with
source ``reprompt``, refuses a stale version with the coded 422, audits a first
acknowledgement, and is rate-limited per vendor user.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select, update

from app.api.deps import create_vendor_access_token
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.vendor_user_legal_acceptance import VendorUserLegalAcceptance
from app.models.workflow import AuditLog
from app.services import password_reset, portal_legal_acceptance
from app.services.legal_acceptance import CURRENT_LEGAL_VERSIONS
from tests.set_password_mail import ACCEPT_CURRENT, ACK_PRIVACY, capture_outbox, link_token
from tests.test_session_management import FakeRedis
from tests.test_vendor_portal_user_password_reset import _create_vendor, _invite_portal_user

# Its portal users start with nothing on record: this file tests the
# unacknowledged path itself (conftest `_test_users_accept_the_legal_versions`).
pytestmark = pytest.mark.legal_unaccepted

TENANT = "a"
RESET = "/api/portal/auth/reset-password"
CHECK = "/api/portal/auth/reset-password/check"
ME = "/api/portal/auth/me"
ACK = "/api/portal/auth/legal-acknowledgement"


@pytest.fixture
def sent_mail(monkeypatch):
    return capture_outbox(monkeypatch)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _invited(realdb, sent_mail) -> tuple[uuid.UUID, str]:
    """An invited supplier — no password yet — and their set-password token."""
    email = f"ack-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        invited = await _invite_portal_user(client, vendor_id, email)
    return uuid.UUID(invited["user"]["id"]), link_token(sent_mail[-1])


async def _rows(realdb, vu_id: uuid.UUID) -> list[VendorUserLegalAcceptance]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (
                await s.execute(
                    select(VendorUserLegalAcceptance).where(
                        VendorUserLegalAcceptance.vendor_user_id == vu_id
                    )
                )
            )
            .scalars()
            .all()
        )


def test_suppliers_are_shown_the_privacy_policy_alone():
    """§499: a Supplier User is not a party to the Terms."""
    assert portal_legal_acceptance.PORTAL_LEGAL_DOCUMENTS == ("privacy",)
    assert portal_legal_acceptance.current_portal_versions() == {
        "privacy": CURRENT_LEGAL_VERSIONS["privacy"]
    }
    assert portal_legal_acceptance.acknowledges_current(CURRENT_LEGAL_VERSIONS["privacy"])
    assert not portal_legal_acceptance.acknowledges_current(None)
    assert not portal_legal_acceptance.acknowledges_current("2000-01-01")


def test_the_table_is_tenant_scoped_and_classified_for_closure():
    from app.services.tenant_provisioning import CONTROL_TABLES
    from app.services.workspace_closure import CLOSURE_ALLOWED

    assert "vendor_user_legal_acceptances" not in CONTROL_TABLES
    assert "vendor_user_legal_acceptances" in CLOSURE_ALLOWED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"accepted_privacy_version": "2000-01-01"},
        # The employee pair's Terms half alone is not an acknowledgement.
        {"accepted_terms_version": CURRENT_LEGAL_VERSIONS["terms"]},
    ],
    ids=["absent", "stale", "terms-only"],
)
async def test_an_invite_link_is_refused_without_the_acknowledgement_and_not_spent(
    realdb, fake_redis, sent_mail, extra
):
    vu_id, token = await _invited(realdb, sent_mail)
    async with realdb.client(key=TENANT, role=None) as anon:
        check = await anon.post(CHECK, json={"token": token})
        assert check.status_code == 200, check.text
        assert check.json()["legal_acceptance_required"] is True

        refused = await anon.post(
            RESET, json={"token": token, "new_password": "Supplier-Chosen9", **extra}
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "privacy_acknowledgement_required"
    # The refusal did not spend the link, set a password, or record anything.
    assert await password_reset.peek_portal_reset_token(token, realdb.info(TENANT).slug) == vu_id
    assert await _rows(realdb, vu_id) == []


@pytest.mark.asyncio
async def test_the_acknowledgement_is_recorded_with_the_password_and_audited(
    realdb, fake_redis, sent_mail
):
    vu_id, token = await _invited(realdb, sent_mail)
    async with realdb.client(key=TENANT, role=None) as anon:
        done = await anon.post(
            RESET, json={"token": token, "new_password": "Supplier-Chosen9", **ACK_PRIVACY}
        )
        assert done.status_code == 200, done.text

    rows = await _rows(realdb, vu_id)
    assert [(r.document, r.version, r.source) for r in rows] == [
        ("privacy", CURRENT_LEGAL_VERSIONS["privacy"], "set_password")
    ]
    async with realdb.sessionmaker(TENANT)() as s:
        audit = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == vu_id, AuditLog.action == "portal.legal.accepted"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audit) == 1
    assert audit[0].details["versions"] == {"privacy": CURRENT_LEGAL_VERSIONS["privacy"]}
    assert audit[0].details["source"] == "set_password"


@pytest.mark.asyncio
async def test_the_employee_pair_is_accepted_but_only_the_privacy_policy_recorded(
    realdb, fake_redis, sent_mail
):
    """A client sending both versions (the employee shape) passes on the
    Privacy Policy half; no Terms row is ever written for a supplier."""
    vu_id, token = await _invited(realdb, sent_mail)
    async with realdb.client(key=TENANT, role=None) as anon:
        done = await anon.post(
            RESET, json={"token": token, "new_password": "Supplier-Chosen9", **ACCEPT_CURRENT}
        )
        assert done.status_code == 200, done.text
    assert [r.document for r in await _rows(realdb, vu_id)] == ["privacy"]


@pytest.mark.asyncio
async def test_a_repeat_keeps_one_row_and_the_first_accepted_at(realdb, fake_redis, sent_mail):
    vu_id, token = await _invited(realdb, sent_mail)
    async with realdb.client(key=TENANT, role=None) as anon:
        assert (
            await anon.post(
                RESET, json={"token": token, "new_password": "Supplier-Chosen9", **ACK_PRIVACY}
            )
        ).status_code == 200
    first = datetime.now(UTC) - timedelta(days=3)
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(VendorUserLegalAcceptance)
            .where(VendorUserLegalAcceptance.vendor_user_id == vu_id)
            .values(accepted_at=first)
        )
        await s.commit()
    async with realdb.sessionmaker(TENANT)() as s:
        await portal_legal_acceptance.record_acknowledgement(s, vendor_user_id=vu_id)
        await s.commit()
    rows = await _rows(realdb, vu_id)
    assert len(rows) == 1
    assert abs((rows[0].accepted_at - first).total_seconds()) < 1


@pytest.mark.asyncio
async def test_a_forgot_password_reset_of_a_password_account_is_not_held_up(
    realdb, fake_redis, sent_mail
):
    vu_id, token = await _invited(realdb, sent_mail)
    async with realdb.client(key=TENANT, role=None) as anon:
        assert (
            await anon.post(
                RESET, json={"token": token, "new_password": "Supplier-Chosen9", **ACK_PRIVACY}
            )
        ).status_code == 200
    # Drop the record so only "has a password" can explain not asking.
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            delete(VendorUserLegalAcceptance).where(
                VendorUserLegalAcceptance.vendor_user_id == vu_id
            )
        )
        await s.commit()
    recovery = await password_reset.issue_portal_reset_token(
        realdb.info(TENANT).slug, vu_id, ttl_seconds=600
    )
    async with realdb.client(key=TENANT, role=None) as anon:
        check = await anon.post(CHECK, json={"token": recovery})
        assert check.json()["legal_acceptance_required"] is False
        done = await anon.post(RESET, json={"token": recovery, "new_password": "Recovered-Pass9"})
        assert done.status_code == 200, done.text
    assert await _rows(realdb, vu_id) == []


@pytest.mark.asyncio
async def test_the_dsar_export_carries_and_erasure_keeps_the_rows(realdb, fake_redis, sent_mail):
    from app.services import privacy_export
    from app.services.privacy_erasure import erase_vendor_user

    vu_id, token = await _invited(realdb, sent_mail)
    org_id = realdb.info(TENANT).org_id
    async with realdb.client(key=TENANT, role=None) as anon:
        assert (
            await anon.post(
                RESET, json={"token": token, "new_password": "Supplier-Chosen9", **ACK_PRIVACY}
            )
        ).status_code == 200

    async with realdb.sessionmaker(TENANT)() as tenant:
        plan = await privacy_export._plan_vendor_user(
            subject_id=vu_id, organization_id=org_id, tenant_db=tenant
        )
    exported = plan.head["legal_acceptances"]
    assert [(e["document"], e["version"], e["source"]) for e in exported] == [
        ("privacy", CURRENT_LEGAL_VERSIONS["privacy"], "set_password")
    ]
    assert privacy_export.bundle_record_counts(plan.head)["legal_acceptances"] == 1

    async with realdb.sessionmaker(TENANT)() as tenant:
        result = await erase_vendor_user(subject_id=vu_id, organization_id=org_id, tenant_db=tenant)
        await tenant.commit()
    assert not result.already_erased
    assert [r.document for r in await _rows(realdb, vu_id)] == ["privacy"]


# ---------------------------------------------------------------------------
# The signed-in re-prompt (§500)
# ---------------------------------------------------------------------------


async def _signed_in_supplier(realdb) -> tuple[uuid.UUID, uuid.UUID]:
    """A supplier who set a password before §499: no acknowledgement on record."""
    org_id = realdb.info(TENANT).org_id
    vendor_id, vu_id = uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name="Reprompt Supply",
                organization_id=org_id,
                status="active",
                source="manual",
            )
        )
        s.add(
            VendorUser(
                id=vu_id,
                vendor_id=vendor_id,
                organization_id=org_id,
                email=f"{vu_id}@portal.test",
                full_name="Portal User",
                hashed_password="x",
                is_active=True,
            )
        )
        await s.commit()
    return vendor_id, vu_id


def _portal_client(realdb, vu_id: uuid.UUID, vendor_id: uuid.UUID):
    client = realdb.client(key=TENANT, role=None)
    client.headers["Authorization"] = f"Bearer {create_vendor_access_token(vu_id, vendor_id)}"
    return client


async def _ack_audit_rows(realdb, vu_id: uuid.UUID) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == vu_id, AuditLog.action == "portal.legal.accepted"
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_me_reports_the_gap_and_the_acknowledgement_closes_it(realdb, fake_redis):
    vendor_id, vu_id = await _signed_in_supplier(realdb)
    async with _portal_client(realdb, vu_id, vendor_id) as c:
        assert (await c.get(ME)).json()["legal_acceptance_required"] is True

        ok = await c.post(ACK, json=ACK_PRIVACY)
        assert ok.status_code == 200, ok.text
        assert ok.json()["legal_acceptance_required"] is False
        assert ok.json()["id"] == str(vu_id)
        assert (await c.get(ME)).json()["legal_acceptance_required"] is False

        # Re-acknowledging is harmless and writes no second row or audit.
        assert (await c.post(ACK, json=ACK_PRIVACY)).status_code == 200

    rows = await _rows(realdb, vu_id)
    assert [(r.document, r.version, r.source) for r in rows] == [
        ("privacy", CURRENT_LEGAL_VERSIONS["privacy"], "reprompt")
    ]
    audit = await _ack_audit_rows(realdb, vu_id)
    assert len(audit) == 1
    assert audit[0].actor_id == vu_id
    assert audit[0].details["source"] == "reprompt"
    assert audit[0].details["versions"] == {"privacy": CURRENT_LEGAL_VERSIONS["privacy"]}


@pytest.mark.asyncio
async def test_every_profile_response_carries_the_flag(realdb, fake_redis):
    """PATCH /me answers with the same profile the portal stores; it must not
    drop the flag (or the locale) and so clear the step without a record."""
    vendor_id, vu_id = await _signed_in_supplier(realdb)
    async with _portal_client(realdb, vu_id, vendor_id) as c:
        patched = await c.patch(ME, json={"locale": "de"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["legal_acceptance_required"] is True
        assert patched.json()["locale"] == "de"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"accepted_privacy_version": "2000-01-01"},
        {"accepted_terms_version": CURRENT_LEGAL_VERSIONS["terms"]},
    ],
    ids=["absent", "stale", "terms-only"],
)
async def test_only_the_version_in_force_is_recorded(realdb, fake_redis, body):
    vendor_id, vu_id = await _signed_in_supplier(realdb)
    async with _portal_client(realdb, vu_id, vendor_id) as c:
        refused = await c.post(ACK, json=body)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "privacy_acknowledgement_required"
        assert (await c.get(ME)).json()["legal_acceptance_required"] is True
    assert await _rows(realdb, vu_id) == []


@pytest.mark.asyncio
async def test_a_version_bump_reopens_the_gap(realdb, fake_redis, monkeypatch):
    vendor_id, vu_id = await _signed_in_supplier(realdb)
    async with _portal_client(realdb, vu_id, vendor_id) as c:
        assert (await c.post(ACK, json=ACK_PRIVACY)).status_code == 200
        monkeypatch.setitem(CURRENT_LEGAL_VERSIONS, "privacy", "2099-01-01")
        assert (await c.get(ME)).json()["legal_acceptance_required"] is True


@pytest.mark.asyncio
async def test_the_record_is_for_the_signed_in_vendor_user_only(realdb, fake_redis):
    """Identity comes from the portal JWT: a body naming another vendor user
    is ignored, and nothing is recorded against them."""
    vendor_id, vu_id = await _signed_in_supplier(realdb)
    _, other_id = await _signed_in_supplier(realdb)
    async with _portal_client(realdb, vu_id, vendor_id) as c:
        ok = await c.post(
            ACK, json={**ACK_PRIVACY, "vendor_user_id": str(other_id), "id": str(other_id)}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["id"] == str(vu_id)
    assert [r.document for r in await _rows(realdb, vu_id)] == ["privacy"]
    assert await _rows(realdb, other_id) == []


@pytest.mark.asyncio
async def test_an_employee_or_anonymous_caller_cannot_record(realdb, fake_redis):
    async with realdb.client(key=TENANT, role=None) as anon:
        assert (await anon.post(ACK, json=ACK_PRIVACY)).status_code == 401
    async with realdb.client(key=TENANT, role="admin") as employee:
        assert (await employee.post(ACK, json=ACK_PRIVACY)).status_code == 401


@pytest.mark.asyncio
async def test_the_acknowledgement_endpoint_is_rate_limited_per_vendor_user(realdb, fake_redis):
    from app.api import portal_auth
    from app.services.rate_limit import RateLimitExceeded

    vendor_id, vu_id = await _signed_in_supplier(realdb)
    limiter = AsyncMock(side_effect=RateLimitExceeded(30))
    with patch("app.api.portal_auth.check_rate_limit", new=limiter):
        async with _portal_client(realdb, vu_id, vendor_id) as c:
            resp = await c.post(ACK, json=ACK_PRIVACY)
    assert resp.status_code == 429, resp.text
    limiter.assert_awaited_once_with(
        "portal_auth_legal_acknowledgement",
        limit=portal_auth.PORTAL_LEGAL_ACKNOWLEDGEMENT_RATE_LIMIT_PER_MINUTE,
        window_seconds=60,
        subject=str(vu_id),
    )
    assert await _rows(realdb, vu_id) == []
