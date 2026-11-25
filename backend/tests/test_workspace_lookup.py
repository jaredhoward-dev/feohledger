"""Self-service "email me my workspace address" — ``POST /api/workspace-lookup``
(`app/api/workspace_lookup.py`, decisions §518).

The properties pinned here are the ones that make a public lookup safe:

- every address gets the same status and body, and the response does not wait
  for anything only a known address does (lookup, send, audit);
- only an active account's own mailbox is emailed, with its own sign-in page;
- the per-IP and per-address budgets and the captcha refuse before any of that;
- the submitted address never reaches a log line.

Real HTTP surface + real Postgres via the `realdb` harness.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import delete, select

from app.api import workspace_lookup as lookup_mod
from app.config import settings
from app.models.organization import Organization
from app.models.user import User
from app.models.workflow import AuditLog
from app.services.email_adapters.console_adapter import ConsoleAdapter
from app.utils.detached_tasks import drain_detached

TENANT = "a"
TEMPLATE = "https://{slug}.feohledger.test"


@pytest.fixture
def sent(monkeypatch):
    """Capture every outbound email instead of printing it."""
    box: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        box.append(message)

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)
    monkeypatch.setattr(settings, "tenant_url_template", TEMPLATE, raising=True)
    return box


@pytest.fixture
async def account(realdb):
    """A throwaway control-plane user in tenant `a` (the seeded role users
    persist across the slot, so nothing here reuses one). Removed afterwards."""
    created: list[uuid.UUID] = []

    async def _make(*, active: bool = True, locale: str | None = None) -> User:
        user = User(
            id=uuid.uuid4(),
            email=f"lookup-{uuid.uuid4().hex[:10]}@example.test",
            full_name="Lookup Test User",
            hashed_password=None,
            is_active=active,
            organization_id=realdb.info(TENANT).org_id,
            must_change_password=False,
            locale=locale,
        )
        async with realdb.control_sessionmaker()() as s:
            s.add(user)
            await s.commit()
        created.append(user.id)
        return user

    yield _make
    if created:
        async with realdb.control_sessionmaker()() as s:
            await s.execute(delete(User).where(User.id.in_(created)))
            await s.commit()


def _unknown() -> str:
    return f"nobody-{uuid.uuid4().hex[:8]}@nowhere.example"


async def _post(realdb, email: str, **extra):
    async with realdb.client(key=TENANT, role=None) as c:
        return await c.post("/api/workspace-lookup", json={"email": email, **extra})


async def _org_name(realdb) -> str:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        return org.name


@pytest.mark.asyncio
async def test_known_and_unknown_addresses_get_the_same_answer(realdb, sent, account):
    user = await account()
    known = await _post(realdb, user.email)
    unknown = await _post(realdb, _unknown())
    await drain_detached()

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert known.json()["status"] == "accepted"
    # Only the account's own mailbox is written to.
    assert [m.to for m in sent] == [user.email]


@pytest.mark.asyncio
async def test_the_email_carries_the_accounts_own_sign_in_page(realdb, sent, account):
    user = await account()
    resp = await _post(realdb, user.email.upper())  # case-insensitive, like sign-in
    await drain_detached()
    assert resp.status_code == 200

    slug = realdb.info(TENANT).slug
    url = f"https://{slug}.feohledger.test/login?email={quote(user.email)}"
    (msg,) = sent
    assert msg.to == user.email
    assert msg.subject == "Your FeohLedger workspace address"
    assert url in msg.body_text
    assert (await _org_name(realdb)) in msg.body_text
    assert "Lookup Test User" in msg.body_text
    assert msg.body_html and url.replace("&", "&amp;") in msg.body_html
    # The preheader names the workspace, never the link.
    assert url not in (msg.preheader or "")


@pytest.mark.asyncio
async def test_the_email_is_in_the_accounts_own_language_first(realdb, sent, account):
    user = await account(locale="de")
    await _post(realdb, user.email, locale="fr")
    await drain_detached()
    (msg,) = sent
    assert msg.locale == "de"
    assert msg.subject == "Die Adresse Ihres FeohLedger-Arbeitsbereichs"


@pytest.mark.asyncio
async def test_a_deactivated_account_is_answered_alike_and_not_emailed(realdb, sent, account):
    user = await account(active=False)
    resp = await _post(realdb, user.email)
    await drain_detached()
    assert resp.status_code == 200
    assert resp.json() == (await _post(realdb, _unknown())).json()
    assert sent == []


@pytest.mark.asyncio
async def test_the_response_does_not_wait_for_the_lookup_or_send(
    realdb, sent, account, monkeypatch
):
    """Response time must not say the address exists: the send is held on a
    gate, and the 200 arrives with nothing sent and no audit row; releasing the
    gate produces both."""
    user = await account()
    gate = asyncio.Event()

    async def _gated_send(self, message):  # noqa: ANN001
        await gate.wait()
        sent.append(message)

    monkeypatch.setattr(ConsoleAdapter, "send", _gated_send, raising=True)

    async def _audit_rows() -> int:
        async with realdb.sessionmaker(TENANT)() as s:
            rows = await s.execute(
                select(AuditLog.id).where(
                    AuditLog.action == lookup_mod.AUDIT_ACTION,
                    AuditLog.entity_id == user.id,
                )
            )
            return len(rows.all())

    try:
        resp = await _post(realdb, user.email)
        assert resp.status_code == 200, resp.text
        assert sent == []
        assert await _audit_rows() == 0
    finally:
        gate.set()
        await drain_detached()

    assert [m.to for m in sent] == [user.email]
    assert await _audit_rows() == 1


@pytest.mark.asyncio
async def test_a_malformed_address_is_refused_before_any_budget(realdb, sent, monkeypatch):
    resp = await _post(realdb, "not-an-address")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "signup_email_invalid"
    assert detail["params"] == {"field": "email"}
    await drain_detached()
    assert sent == []


@pytest.mark.asyncio
async def test_per_ip_budget(realdb, sent, monkeypatch):
    monkeypatch.setattr(settings, "workspace_lookup_rate_limit_per_hour", 2)
    statuses = []
    async with realdb.client(key=TENANT, role=None) as c:
        for _ in range(3):
            r = await c.post("/api/workspace-lookup", json={"email": _unknown()})
            statuses.append(r.status_code)
    assert statuses == [200, 200, 429]
    assert r.json()["detail"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_per_address_budget_holds_across_ips(realdb, sent, account, monkeypatch):
    """One address is capped even when the per-IP budget is not the limit —
    and the cap is the same for a known address as for an unknown one."""
    monkeypatch.setattr(settings, "workspace_lookup_rate_limit_per_hour", 50)
    monkeypatch.setattr(settings, "workspace_lookup_email_rate_limit_per_hour", 2)
    user = await account()
    victim_unknown = _unknown()
    async with realdb.client(key=TENANT, role=None) as c:
        known = [
            (await c.post("/api/workspace-lookup", json={"email": user.email})).status_code
            for _ in range(3)
        ]
        unknown = [
            (await c.post("/api/workspace-lookup", json={"email": victim_unknown})).status_code
            for _ in range(3)
        ]
        # Case and padding do not open a fresh budget.
        padded = await c.post("/api/workspace-lookup", json={"email": f"  {user.email.upper()} "})
        other = await c.post("/api/workspace-lookup", json={"email": _unknown()})
    await drain_detached()
    assert known == unknown == [200, 200, 429]
    assert padded.status_code == 429
    assert other.status_code == 200
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_captcha_is_required_when_configured(realdb, sent, account, monkeypatch):
    """With a secret configured, a request with no token is refused 400
    `captcha_required` (the same `verify_captcha` as `/signup/start`) and
    nothing is looked up or sent."""
    monkeypatch.setattr(settings, "hcaptcha_secret", "0xtest-secret", raising=True)
    user = await account()
    resp = await _post(realdb, user.email)
    await drain_detached()
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "captcha_required"
    assert sent == []


@pytest.mark.asyncio
async def test_a_failed_captcha_is_refused(realdb, sent, account, monkeypatch):
    from app.utils.hcaptcha import CaptchaError

    async def _fail(token, ip):  # noqa: ARG001
        raise CaptchaError("Captcha verification failed. Please try again.")

    monkeypatch.setattr(lookup_mod, "verify_captcha", _fail)
    user = await account()
    resp = await _post(realdb, user.email, captcha_token="bad")
    await drain_detached()
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "captcha_failed"
    assert sent == []


@pytest.mark.asyncio
async def test_the_address_never_reaches_a_log_line(realdb, account, monkeypatch, caplog):
    """Neither a send that fails with the address in its message, nor a
    successful send, nor an unknown address logs what was submitted."""
    monkeypatch.setattr(settings, "tenant_url_template", TEMPLATE, raising=True)
    user = await account()
    unknown = _unknown()

    async def _refuse(self, message):  # noqa: ANN001
        raise RuntimeError(f"recipient refused: {message.to}")

    monkeypatch.setattr(ConsoleAdapter, "send", _refuse, raising=True)
    caplog.set_level(logging.DEBUG)
    assert (await _post(realdb, user.email)).status_code == 200
    assert (await _post(realdb, unknown)).status_code == 200
    await drain_detached()

    assert "RuntimeError" in caplog.text  # the failure was logged, by class
    assert user.email not in caplog.text
    assert unknown not in caplog.text
    assert user.email.split("@")[0] not in caplog.text
