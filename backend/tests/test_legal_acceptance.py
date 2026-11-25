"""Acceptance of our Terms of Service and Privacy Policy (`docs/decisions.md` §495).

Pins: the versions source of truth and its frontend drift guard; signup refuses
without acceptance of the versions in force and records it against the admin it
provisions; a set-password link (an account with no password yet) carries the
acceptance and a forgot-password reset of a password account is not held up;
`/me` reports `legal_acceptance_required` and `POST /auth/legal-acceptance`
clears it (rate-limited per account); a repeat acceptance keeps the first
`accepted_at`; the DSAR export carries the rows; a DSAR erasure and a workspace
deletion keep them.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.models.legal_acceptance import LegalAcceptance
from app.models.signup import EmailVerification
from app.models.user import User
from app.services import legal_acceptance, password_reset
from app.services.legal_acceptance import CURRENT_LEGAL_VERSIONS
from app.services.tenant_deletion import CONTROL_DELETIONS, CONTROL_TABLES_EXEMPT
from app.services.tenant_provisioning import CONTROL_TABLES, ProvisioningResult
from tests.set_password_mail import ACCEPT_CURRENT


def _gen_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "gen_legal_versions.py"
    spec = importlib.util.spec_from_file_location("gen_legal_versions", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Source of truth + drift guard (no database)
# ---------------------------------------------------------------------------


def test_accepts_current_only_matches_the_versions_in_force():
    assert legal_acceptance.accepts_current(
        CURRENT_LEGAL_VERSIONS["terms"], CURRENT_LEGAL_VERSIONS["privacy"]
    )
    assert not legal_acceptance.accepts_current(None, None)
    assert not legal_acceptance.accepts_current(CURRENT_LEGAL_VERSIONS["terms"], None)
    assert not legal_acceptance.accepts_current("2000-01-01", CURRENT_LEGAL_VERSIONS["privacy"])


def test_the_frontend_version_module_is_in_sync():
    assert _gen_module().main(["--check"]) == 0


def test_the_drift_guard_fails_on_a_stale_module(monkeypatch):
    mod = _gen_module()
    monkeypatch.setitem(mod.CURRENT_LEGAL_VERSIONS, "terms", "2099-01-01")
    assert mod.main(["--check"]) == 1


def test_the_generated_dart_carries_the_versions_in_force():
    mod = _gen_module()
    dart = mod.render_dart()
    assert f"const String kTermsVersion = '{CURRENT_LEGAL_VERSIONS['terms']}';" in dart
    assert f"const String kPrivacyVersion = '{CURRENT_LEGAL_VERSIONS['privacy']}';" in dart
    # And the committed file the mobile app builds from is exactly that.
    committed = mod._repo_root() / mod.DART_OUTPUT_PATH
    assert committed.read_text(encoding="utf-8") == dart


def test_the_drift_guard_fails_when_only_the_dart_file_drifts(monkeypatch, tmp_path):
    # A copy of the repo's two generated files, so the committed mobile file is
    # never touched: the TypeScript one in sync, the Dart one hand-edited.
    mod = _gen_module()
    for rel, _ in mod.OUTPUTS:
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_text((mod._repo_root() / rel).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "_repo_root", lambda: tmp_path)
    assert mod.main(["--check"]) == 0

    dart = tmp_path / mod.DART_OUTPUT_PATH
    dart.write_text(
        dart.read_text(encoding="utf-8").replace(CURRENT_LEGAL_VERSIONS["privacy"], "2000-01-01"),
        encoding="utf-8",
    )
    assert mod.main(["--check"]) == 1

    dart.unlink()
    assert mod.main(["--check"]) == 1


def test_a_version_that_is_not_a_date_is_refused(monkeypatch):
    mod = _gen_module()
    monkeypatch.setitem(mod.CURRENT_LEGAL_VERSIONS, "privacy", "v2")
    with pytest.raises(ValueError):
        mod.render()


def test_the_table_is_control_plane_and_survives_workspace_deletion():
    assert "legal_acceptances" in CONTROL_TABLES
    assert "legal_acceptances" in CONTROL_TABLES_EXEMPT
    assert "legal_acceptances" not in {name for name, _ in CONTROL_DELETIONS}


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def _start_body(slug: str, **overrides) -> dict:
    return {
        "company_name": "Accept Co",
        "slug": slug,
        "admin_name": "Accept Tester",
        "admin_email": f"{slug}@example.com",
        "captcha_token": "ignored-in-dev",
        **ACCEPT_CURRENT,
        **overrides,
    }


async def _rows_for(realdb, user_id: uuid.UUID) -> list[LegalAcceptance]:
    async with realdb.control_sessionmaker()() as s:
        return list(
            (
                await s.execute(
                    select(LegalAcceptance)
                    .where(LegalAcceptance.user_id == user_id)
                    .order_by(LegalAcceptance.document)
                )
            )
            .scalars()
            .all()
        )


async def _drop_rows(realdb, user_id: uuid.UUID) -> None:
    async with realdb.control_sessionmaker()() as s:
        await s.execute(delete(LegalAcceptance).where(LegalAcceptance.user_id == user_id))
        await s.commit()


@pytest.mark.parametrize(
    "overrides",
    [
        {"accepted_terms_version": None, "accepted_privacy_version": None},
        {"accepted_privacy_version": None},
        {"accepted_terms_version": "2000-01-01"},
    ],
    ids=["neither", "terms-only", "stale-terms"],
)
async def test_signup_start_refuses_without_acceptance_of_the_current_versions(realdb, overrides):
    slug = "acc" + uuid.uuid4().hex[:10]
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/signup/start", json=_start_body(slug, **overrides))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "legal_acceptance_required"
    async with realdb.control_sessionmaker()() as s:
        assert (
            await s.execute(select(EmailVerification).where(EmailVerification.slug == slug))
        ).first() is None


async def test_signup_records_the_acceptance_against_the_provisioned_admin(realdb, monkeypatch):
    from app.api import signup as signup_mod

    slug = "acc" + uuid.uuid4().hex[:10]
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(
        signup_mod,
        "provision_tenant",
        _async_return(ProvisioningResult(organization_id=org_id, user_id=user_id, db_name="x")),
    )
    try:
        async with realdb.client(key="a", role=None) as c:
            started = await c.post("/api/signup/start", json=_start_body(slug))
            assert started.status_code == 200, started.text
            async with realdb.control_sessionmaker()() as s:
                token = (
                    await s.execute(
                        select(EmailVerification.token).where(EmailVerification.slug == slug)
                    )
                ).scalar_one()
            done = await c.post("/api/signup/complete", json={"token": token})
            assert done.status_code == 200, done.text

        rows = await _rows_for(realdb, user_id)
        assert [(r.document, r.version, r.source) for r in rows] == [
            ("privacy", CURRENT_LEGAL_VERSIONS["privacy"], "signup"),
            ("terms", CURRENT_LEGAL_VERSIONS["terms"], "signup"),
        ]
        assert {r.organization_id for r in rows} == {org_id}
    finally:
        await _drop_rows(realdb, user_id)
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(EmailVerification).where(EmailVerification.slug == slug))
            await s.commit()


def _async_return(value):
    async def _f(**_kwargs):
        return value

    return _f


# ---------------------------------------------------------------------------
# Set-password vs forgot-password
# ---------------------------------------------------------------------------


@pytest.fixture
def session_redis(monkeypatch):
    """The zset+hash fake `/auth/reset-password`'s session revocation needs —
    the same override `test_signup.py::session_redis` installs."""
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _user(realdb, *, password: str | None) -> uuid.UUID:
    from app.utils.passwords import hash_password

    user_id = uuid.uuid4()
    async with realdb.control_sessionmaker()() as s:
        s.add(
            User(
                id=user_id,
                email=f"accept-{uuid.uuid4().hex[:10]}@example.com",
                full_name="Accept Tester",
                hashed_password=await hash_password(password) if password else None,
                is_active=True,
                organization_id=realdb.info("a").org_id,
                must_change_password=False,
            )
        )
        await s.commit()
    return user_id


async def _drop_user(realdb, user_id: uuid.UUID) -> None:
    await _drop_rows(realdb, user_id)
    async with realdb.control_sessionmaker()() as s:
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def test_a_set_password_link_requires_acceptance_and_survives_a_refusal(
    realdb, session_redis
):
    user_id = await _user(realdb, password=None)
    token = await password_reset.issue_reset_token(user_id)
    try:
        async with realdb.client(key="a", role=None) as c:
            check = await c.post("/api/auth/reset-password/check", json={"token": token})
            assert check.status_code == 200, check.text
            assert check.json()["legal_acceptance_required"] is True

            refused = await c.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "Chosen-Accept9"},
            )
            assert refused.status_code == 422, refused.text
            assert refused.json()["detail"]["code"] == "legal_acceptance_required"
            # The refusal did not spend the link.
            assert await password_reset.peek_reset_token(token) == user_id

            done = await c.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "Chosen-Accept9", **ACCEPT_CURRENT},
            )
            assert done.status_code == 200, done.text

        rows = await _rows_for(realdb, user_id)
        assert {(r.document, r.version, r.source) for r in rows} == {
            ("terms", CURRENT_LEGAL_VERSIONS["terms"], "set_password"),
            ("privacy", CURRENT_LEGAL_VERSIONS["privacy"], "set_password"),
        }
    finally:
        await _drop_user(realdb, user_id)


async def test_a_forgot_password_reset_of_a_password_account_is_not_held_up(realdb, session_redis):
    user_id = await _user(realdb, password="Existing-Pass9x")
    token = await password_reset.issue_reset_token(user_id)
    try:
        async with realdb.client(key="a", role=None) as c:
            check = await c.post("/api/auth/reset-password/check", json={"token": token})
            assert check.json()["legal_acceptance_required"] is False
            done = await c.post(
                "/api/auth/reset-password",
                json={"token": token, "new_password": "Recovered-Pass9"},
            )
            assert done.status_code == 200, done.text
        assert await _rows_for(realdb, user_id) == []
    finally:
        await _drop_user(realdb, user_id)


# ---------------------------------------------------------------------------
# Re-prompt
# ---------------------------------------------------------------------------


async def test_me_reports_the_gap_and_the_accept_endpoint_closes_it(realdb):
    admin_id = realdb.info("a").users["admin"]
    await _drop_rows(realdb, admin_id)
    try:
        async with realdb.client(key="a", role="admin") as c:
            me = await c.get("/api/auth/me")
            assert me.json()["legal_acceptance_required"] is True

            stale = await c.post(
                "/api/auth/legal-acceptance",
                json={"accepted_terms_version": "2000-01-01", "accepted_privacy_version": None},
            )
            assert stale.status_code == 422
            assert stale.json()["detail"]["code"] == "legal_acceptance_required"

            ok = await c.post("/api/auth/legal-acceptance", json=ACCEPT_CURRENT)
            assert ok.status_code == 200, ok.text
            assert ok.json()["legal_acceptance_required"] is False
            again = await c.post("/api/auth/legal-acceptance", json=ACCEPT_CURRENT)
            assert again.status_code == 200
        rows = await _rows_for(realdb, admin_id)
        # Append-only: re-accepting did not add or rewrite a row.
        assert len(rows) == 2
        assert {r.source for r in rows} == {"reprompt"}
    finally:
        await _drop_rows(realdb, admin_id)


async def test_a_version_bump_reopens_the_gap(realdb, monkeypatch):
    admin_id = realdb.info("a").users["admin"]
    try:
        async with realdb.client(key="a", role="admin") as c:
            assert (
                await c.post("/api/auth/legal-acceptance", json=ACCEPT_CURRENT)
            ).status_code == 200
            monkeypatch.setitem(CURRENT_LEGAL_VERSIONS, "terms", "2099-01-01")
            me = await c.get("/api/auth/me")
            assert me.json()["legal_acceptance_required"] is True
    finally:
        await _drop_rows(realdb, admin_id)


async def test_the_accept_endpoint_is_rate_limited_per_account(realdb):
    from app.api import auth as auth_mod
    from app.services.rate_limit import RateLimitExceeded

    admin_id = realdb.info("a").users["admin"]
    await _drop_rows(realdb, admin_id)
    limiter = AsyncMock(side_effect=RateLimitExceeded(30))
    try:
        with patch("app.api.auth.check_rate_limit", new=limiter):
            async with realdb.client(key="a", role="admin") as c:
                resp = await c.post("/api/auth/legal-acceptance", json=ACCEPT_CURRENT)
        assert resp.status_code == 429, resp.text
        limiter.assert_awaited_once_with(
            "auth_legal_acceptance",
            limit=auth_mod.LEGAL_ACCEPTANCE_RATE_LIMIT_PER_MINUTE,
            window_seconds=60,
            subject=str(admin_id),
        )
        # Refused before anything was written.
        assert await _rows_for(realdb, admin_id) == []
    finally:
        await _drop_rows(realdb, admin_id)


# ---------------------------------------------------------------------------
# Append-only record
# ---------------------------------------------------------------------------


async def test_a_repeat_acceptance_keeps_one_row_and_the_first_accepted_at(realdb):
    user_id = await _user(realdb, password=None)
    first = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    try:
        for when, source in ((first, "signup"), (first + timedelta(days=3), "reprompt")):
            async with realdb.control_sessionmaker()() as s:
                await legal_acceptance.record_acceptances(
                    s,
                    user_id=user_id,
                    organization_id=realdb.info("a").org_id,
                    versions=CURRENT_LEGAL_VERSIONS,
                    source=source,
                    accepted_at=when,
                )
                await s.commit()
        rows = await _rows_for(realdb, user_id)
        assert len(rows) == 2  # one per document, not one per call
        assert {r.accepted_at for r in rows} == {first}
        assert {r.source for r in rows} == {"signup"}
    finally:
        await _drop_user(realdb, user_id)


# ---------------------------------------------------------------------------
# DSAR export + erasure
# ---------------------------------------------------------------------------


async def test_the_dsar_export_carries_the_acceptances(realdb):
    from app.services import privacy_export

    user_id = await _user(realdb, password=None)
    try:
        async with realdb.control_sessionmaker()() as s:
            await legal_acceptance.record_acceptances(
                s,
                user_id=user_id,
                organization_id=realdb.info("a").org_id,
                versions=CURRENT_LEGAL_VERSIONS,
                source="signup",
            )
            await s.commit()
        async with (
            realdb.control_sessionmaker()() as control,
            realdb.sessionmaker("a")() as tenant,
        ):
            plan = await privacy_export._plan_user(
                subject_id=user_id,
                organization_id=realdb.info("a").org_id,
                control_db=control,
                tenant_db=tenant,
            )
        exported = plan.head["legal_acceptances"]
        assert {(e["document"], e["version"], e["source"]) for e in exported} == {
            ("terms", CURRENT_LEGAL_VERSIONS["terms"], "signup"),
            ("privacy", CURRENT_LEGAL_VERSIONS["privacy"], "signup"),
        }
    finally:
        await _drop_user(realdb, user_id)


async def test_a_dsar_erasure_keeps_the_acceptances(realdb):
    """§495: the rows are the evidence a contract was formed, kept under
    Art. 17(3)(e); once the user row is redacted they identify no one."""
    from app.services.privacy_erasure import erase_user

    user_id = await _user(realdb, password=None)
    org_id = realdb.info("a").org_id
    try:
        async with realdb.control_sessionmaker()() as s:
            await legal_acceptance.record_acceptances(
                s,
                user_id=user_id,
                organization_id=org_id,
                versions=CURRENT_LEGAL_VERSIONS,
                source="signup",
            )
            await s.commit()
        async with realdb.control_sessionmaker()() as s:
            result = await erase_user(subject_id=user_id, organization_id=org_id, control_db=s)
            await s.commit()
        assert not result.already_erased
        async with realdb.control_sessionmaker()() as s:
            erased = await s.get(User, user_id)
            assert erased.email.startswith("erased+")
        rows = await _rows_for(realdb, user_id)
        assert {(r.document, r.version, r.source) for r in rows} == {
            ("terms", CURRENT_LEGAL_VERSIONS["terms"], "signup"),
            ("privacy", CURRENT_LEGAL_VERSIONS["privacy"], "signup"),
        }
    finally:
        await _drop_user(realdb, user_id)
