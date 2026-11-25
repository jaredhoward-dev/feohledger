"""Webhook security primitives + integration tests.

Project invariant #9: "Webhook handlers verify signatures and dedupe
by event id." The shared `services/webhook_security.py` is the home
for the HMAC + dedup helpers; this test file pins:

  - HMAC primitive uses constant-time compare + SHA-256
  - Dedup uses Redis SET-IF-NOT-EXISTS with TTL (replays caught
    inside the window, expired entries free up)
  - Card webhook handler refuses unsigned / wrong-sig requests
  - Card webhook deduplicates by event id across deliveries
  - ERP webhook handler refuses unsigned / wrong-sig requests
  - ERP webhook deduplicates by event id
  - Both webhooks return 204 silently on every rejection path
    (no enumeration of tenant slugs / card tokens)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.webhook_security import DEDUP_PREFIX

# The fake org's settings carry the webhook secrets; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

# ---------------------------------------------------------------------------
# Primitive: verify_hmac_sha256
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_verify_hmac_accepts_correctly_signed_body():
    from app.services.webhook_security import verify_hmac_sha256

    body = b'{"event":"settled"}'
    sig = _sign("s3cret", body)
    assert verify_hmac_sha256("s3cret", body, sig) is True


def test_verify_hmac_rejects_wrong_signature():
    from app.services.webhook_security import verify_hmac_sha256

    body = b'{"event":"settled"}'
    # Off-by-one signature
    bad_sig = _sign("s3cret", body)[:-1] + ("A" if _sign("s3cret", body)[-1] != "A" else "B")
    assert verify_hmac_sha256("s3cret", body, bad_sig) is False


def test_verify_hmac_rejects_when_body_was_tampered():
    """If an attacker modifies the body after signing, the HMAC must
    fail — that's the whole point of signing the bytes-on-the-wire."""
    from app.services.webhook_security import verify_hmac_sha256

    sig = _sign("s3cret", b'{"event":"settled","amount":100}')
    # Same key, different body — sig won't match.
    assert verify_hmac_sha256("s3cret", b'{"event":"settled","amount":99999}', sig) is False


def test_verify_hmac_rejects_when_secret_is_empty():
    """An unconfigured secret must NEVER be the green light — without
    this check, "no secret = anyone can post" would be the default."""
    from app.services.webhook_security import verify_hmac_sha256

    body = b'{"x":1}'
    sig = _sign("anything", body)
    assert verify_hmac_sha256("", body, sig) is False


def test_verify_hmac_rejects_when_signature_is_missing():
    from app.services.webhook_security import verify_hmac_sha256

    assert verify_hmac_sha256("s3cret", b'{"x":1}', None) is False
    assert verify_hmac_sha256("s3cret", b'{"x":1}', "") is False


def test_verify_hmac_uses_constant_time_compare():
    """`hmac.compare_digest` is the documented constant-time
    comparator. A regression that swapped it for `==` would leak
    the signature one character at a time via response timing."""
    import inspect

    from app.services import webhook_security

    src = inspect.getsource(webhook_security.verify_hmac_sha256)
    assert "compare_digest" in src, "verify_hmac_sha256 must use hmac.compare_digest"


# ---------------------------------------------------------------------------
# Primitive: extract_signature_header
# ---------------------------------------------------------------------------


def test_extract_signature_header_is_case_insensitive():
    """HTTP headers are case-insensitive. The helper must find the
    signature regardless of how the provider cased its header."""
    from app.services.webhook_security import extract_signature_header

    headers = {"webhook-signature": "sig123"}
    assert extract_signature_header(headers, "Webhook-Signature") == "sig123"

    headers = {"WEBHOOK-SIGNATURE": "sig456"}
    assert extract_signature_header(headers, "Webhook-Signature") == "sig456"


def test_extract_signature_header_picks_first_present_candidate():
    """Some providers ship under multiple legacy names. The helper
    walks candidates in order — first hit wins."""
    from app.services.webhook_security import extract_signature_header

    headers = {"x-signature": "v1", "webhook-signature": "v2"}
    assert extract_signature_header(headers, "Webhook-Signature", "X-Signature") == "v2"


def test_extract_signature_header_returns_none_when_no_match():
    from app.services.webhook_security import extract_signature_header

    assert extract_signature_header({}, "Webhook-Signature") is None
    assert extract_signature_header({"other": "x"}, "Webhook-Signature") is None


# ---------------------------------------------------------------------------
# Primitive: is_event_already_processed (Redis dedup)
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, tuple[float | None, str]] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None  # already set
        expiry = time.time() + ex if ex else None
        self.store[key] = (expiry, value)
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    return fake


@pytest.mark.asyncio
async def test_dedup_first_call_returns_false_subsequent_returns_true(fake_redis):
    """The semantics: first delivery returns False ("you're first,
    process it"); every retry within TTL returns True ("already done,
    no-op"). A regression that flipped the polarity would cause every
    event to be ignored OR every retry to be re-processed."""
    from app.services.webhook_security import is_event_already_processed

    assert await is_event_already_processed("lithic", "evt_1") is False
    assert await is_event_already_processed("lithic", "evt_1") is True
    assert await is_event_already_processed("lithic", "evt_1") is True


@pytest.mark.asyncio
async def test_dedup_scopes_by_provider(fake_redis):
    """Different providers shouldn't collide. Lithic's `evt_42`
    and Nium's `evt_42` are unrelated events."""
    from app.services.webhook_security import is_event_already_processed

    assert await is_event_already_processed("lithic", "evt_42") is False
    # Same event id, different provider → independent bucket.
    assert await is_event_already_processed("nium", "evt_42") is False


@pytest.mark.asyncio
async def test_dedup_returns_false_when_event_id_is_empty(fake_redis):
    """An empty event id can't be deduped — every "no id" delivery
    must be treated as first-time. Logging happens but the helper
    doesn't fail closed (provider quirks shouldn't blackhole events)."""
    from app.services.webhook_security import is_event_already_processed

    assert await is_event_already_processed("lithic", "") is False
    assert await is_event_already_processed("lithic", None) is False


# ---------------------------------------------------------------------------
# Card webhook integration
# ---------------------------------------------------------------------------


def _fake_request(body: bytes, headers: dict | None = None):
    req = MagicMock()
    req.body = AsyncMock(return_value=body)

    async def _json():
        import json

        return json.loads(body.decode("utf-8"))

    req.json = _json
    req.headers = headers or {}
    return req


def _fake_ctrl_session_factory(orgs):
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=orgs)
    result.scalars = MagicMock(return_value=scalars)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _fake_tenant_session_factory(card):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=card)
    db = AsyncMock()
    # SQLAlchemy's Session.add is synchronous — override to a plain
    # MagicMock so the handler's `db.add(rebate)` call doesn't leak
    # an unawaited-coroutine RuntimeWarning.
    db.add = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    # Session.begin_nested() is synchronous and returns an async context
    # manager (the savepoint). A plain MagicMock supports the async-CM
    # protocol; leaving it as an AsyncMock child would return a coroutine
    # and blow up `async with db.begin_nested():` — the rebate savepoint the
    # settled-event path opens for the one-rebate-per-card backstop.
    db.begin_nested = MagicMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, db


def _org_with_card_secret(secret: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"cards": {"webhook_signing_secret": secret}},
    )


def _card_row(provider_card_id: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider_card_id=provider_card_id,
        status="created",
        amount_limit=100,
        amount_charged=None,
        charged_at=None,
        # Stamped by the first authorization and never cleared (decisions §549).
        first_authorized_at=None,
        merchant_name=None,
        organization_id=uuid.uuid4(),
        # The webhook writes a `card.charged`/`card.settled` audit row that reads
        # these fields — the fake card must provide them, or the handler's audit
        # call raises and rolls back instead of committing.
        correlation_id=uuid.uuid4(),
        last_four="4242",
        # `VirtualCard.currency` is a non-nullable column with a "USD" default,
        # so a real row ALWAYS has one. The webhook reads it to pick the
        # ISO-4217 minor-unit exponent; without it the handler raises
        # AttributeError, which the broad `except` turns into a silent 204 —
        # the mutation is applied in memory but never committed, so the test
        # sees a "charged" card and no commit.
        currency="USD",
        # `VirtualCard.payment_id` is a nullable column every real row carries;
        # settlement reads it to write the charge onto a run's card leg
        # (decisions §543). None: a `/cards/generate` card, no run payment.
        payment_id=None,
    )


# Each provider is verified the way it really authenticates (decisions §555):
# Lithic — Standard-Webhooks HMAC over `id.timestamp.body`, `whsec_` key, five
# minute window (https://docs.lithic.com/docs/events-api); Nium — the static
# `x-partner-key` header, deduped on `x-request-id`
# (https://docs.nium.com/docs/developers/notifications-and-webhooks).
_LITHIC_SECRET = "whsec_" + base64.b64encode(b"lithic-test-signing-key-0001").decode()
_NIUM_PARTNER_KEY = "nium-partner-key-0001"


def _lithic_body(status: str = "PENDING", *, event_type: str = "card_transaction.updated") -> bytes:
    """`card_transaction.updated` as Lithic sends it: the Transaction object
    plus a top-level `event_type` (lithic-openapi.yml)."""
    return json.dumps(
        {
            "event_type": event_type,
            "token": "7ff4e46b-2cae-4c0b-8f1b-f2a0b4d4c0de",
            "card_token": "card_token_abc",
            "account_token": "bd5e5649-1be8-4117-9bc5-3268258d1417",
            "status": status,
            "result": "APPROVED",
            "amount": 5000,
            "amounts": {
                "hold": {"amount": 5000, "currency": "USD"},
                "settlement": {"amount": 5000 if status == "SETTLED" else 0, "currency": "USD"},
            },
            "merchant": {"descriptor": "ACME FUEL"},
            "created": "2026-11-25T12:00:00Z",
        }
    ).encode("utf-8")


def _lithic_headers(
    body: bytes,
    *,
    msg_id: str = "65a9dad4-1b60-4686-83fd-65b25078a4b4",
    secret: str = _LITHIC_SECRET,
    timestamp: int | None = None,
) -> dict:
    from app.services.webhook_security import sign_standard_webhook

    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sign_standard_webhook(secret, msg_id, ts, body),
    }


def _nium_body(template: str = "CARD_POS_APPROVED_WEBHOOK") -> bytes:
    """A Nium card webhook as its docs show it: the event named in `template`."""
    return json.dumps(
        {
            "customerHashId": "0de1e512-e0d7-4eca-ad41-dd39325facc2",
            "walletHashId": "696bad93-bda7-4e8d-8c31-318f8d6cbc5f",
            "cardHashId": "card_token_abc",
            "transactionCurrency": "USD",
            "transactionAmount": "50.00",
            "authAmount": "50.00",
            "merchantName": "ACME FUEL",
            "authCode": "114733",
            "template": template,
        }
    ).encode("utf-8")


def _nium_headers(*, request_id: str = "123e4567-e89b-12d3-a456-426655440000", key=None) -> dict:
    return {
        "x-request-id": request_id,
        "x-partner-key": _NIUM_PARTNER_KEY if key is None else key,
    }


async def _deliver(provider: str, secret: str, body: bytes, headers: dict, card):
    """Post one delivery through the real handler against a fake tenant
    holding ``card``; returns (result, the tenant session)."""
    from app.api.cards import card_webhook

    org = _org_with_card_secret(secret)
    tenant_factory, db = _fake_tenant_session_factory(card)
    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        result = await card_webhook(provider=provider, request=_fake_request(body, headers))
    return result, db


# --- the shared verifier ----------------------------------------------------


def test_standard_webhook_accepts_lithic_signature_and_any_rotated_one():
    from app.services.webhook_security import sign_standard_webhook, verify_standard_webhook

    body = _lithic_body()
    ts = int(time.time())
    good = sign_standard_webhook(_LITHIC_SECRET, "msg_1", ts, body)
    assert verify_standard_webhook(_LITHIC_SECRET, "msg_1", str(ts), body, good)
    # During a rotation the header lists several; any one matching suffices.
    assert verify_standard_webhook(_LITHIC_SECRET, "msg_1", str(ts), body, f"v1,AAAA {good} v2,zzz")


def test_standard_webhook_matches_the_published_reference_vector():
    """The Standard-Webhooks reference example (the Svix scheme Lithic uses):
    an independently computed signature, not our own signer."""
    from app.services.webhook_security import verify_standard_webhook

    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    msg_id = "msg_p5jXN8AQM9LWM0D4loKWxJek"
    ts = "1614265330"
    body = b'{"test": 2432232314}'
    sig = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    assert verify_standard_webhook(secret, msg_id, ts, body, sig, now=1614265330)
    assert not verify_standard_webhook(secret, msg_id, ts, body + b" ", sig, now=1614265330)


@pytest.mark.parametrize(
    "mutate",
    [
        "wrong_secret",
        "tampered_body",
        "other_id",
        "stale",
        "future",
        "no_prefix_secret",
        "no_v1",
        "non_numeric_ts",
        "missing_sig",
        "missing_ts",
        "missing_id",
    ],
)
def test_standard_webhook_fails_closed(mutate):
    from app.services.webhook_security import sign_standard_webhook, verify_standard_webhook

    body = _lithic_body()
    now = 1_800_000_000
    ts = now
    secret, msg_id, ts_text, sig = (
        _LITHIC_SECRET,
        "msg_1",
        str(ts),
        sign_standard_webhook(_LITHIC_SECRET, "msg_1", ts, body),
    )
    verify_body = body
    if mutate == "wrong_secret":
        secret = "whsec_" + base64.b64encode(b"attacker").decode()
    elif mutate == "tampered_body":
        verify_body = body.replace(b"5000", b"9999")
    elif mutate == "other_id":
        msg_id = "msg_2"
    elif mutate == "stale":
        now = ts + 301
    elif mutate == "future":
        now = ts - 301
    elif mutate == "no_prefix_secret":
        secret = _LITHIC_SECRET.removeprefix("whsec_")
    elif mutate == "no_v1":
        sig = sig.replace("v1,", "v2,")
    elif mutate == "non_numeric_ts":
        ts_text = "1.8e9"
    elif mutate == "missing_sig":
        sig = None
    elif mutate == "missing_ts":
        ts_text = None
    elif mutate == "missing_id":
        msg_id = None
    assert not verify_standard_webhook(secret, msg_id, ts_text, verify_body, sig, now=now)


def test_standard_webhook_accepts_inside_the_window():
    from app.services.webhook_security import sign_standard_webhook, verify_standard_webhook

    body = _lithic_body()
    ts = 1_800_000_000
    sig = sign_standard_webhook(_LITHIC_SECRET, "m", ts, body)
    assert verify_standard_webhook(_LITHIC_SECRET, "m", str(ts), body, sig, now=ts + 300)
    assert verify_standard_webhook(_LITHIC_SECRET, "m", str(ts), body, sig, now=ts - 300)


def test_shared_header_token_is_exact_and_fails_closed():
    from app.services.webhook_security import verify_shared_header_token

    assert verify_shared_header_token("k1", "k1")
    assert not verify_shared_header_token("k1", "k2")
    assert not verify_shared_header_token("k1", "k1 ")
    assert not verify_shared_header_token("", "")
    assert not verify_shared_header_token("k1", None)


# --- Lithic through the handler ---------------------------------------------


@pytest.mark.asyncio
async def test_lithic_correctly_signed_real_shape_event_charges_the_card(fake_redis):
    """Positive control: without it the negatives could pass because every
    request is rejected."""
    body = _lithic_body("PENDING")
    card = _card_row("card_token_abc")
    result, db = await _deliver("lithic", _LITHIC_SECRET, body, _lithic_headers(body), card)
    assert result is None
    assert card.status == "charged"
    db.commit.assert_called()
    # Deduped on Lithic's own event id, the `webhook-id` header.
    assert f"{DEDUP_PREFIX}lithic:65a9dad4-1b60-4686-83fd-65b25078a4b4" in fake_redis.store


@pytest.mark.parametrize(
    "headers_for",
    [
        pytest.param(lambda body: {}, id="unsigned"),
        pytest.param(
            lambda body: _lithic_headers(
                body, secret="whsec_" + base64.b64encode(b"attacker-guess").decode()
            ),
            id="wrong-secret",
        ),
        pytest.param(
            lambda body: _lithic_headers(body, timestamp=int(time.time()) - 600), id="stale"
        ),
        pytest.param(
            lambda body: {
                k: v for k, v in _lithic_headers(body).items() if k != "webhook-timestamp"
            },
            id="no-timestamp",
        ),
        pytest.param(
            lambda body: {k: v for k, v in _lithic_headers(body).items() if k != "webhook-id"},
            id="no-id",
        ),
        pytest.param(
            lambda body: {"Webhook-Signature": _sign(_LITHIC_SECRET, body)},
            id="old-hex-hmac-scheme",
        ),
    ],
)
@pytest.mark.asyncio
async def test_lithic_unverifiable_delivery_is_a_silent_204_and_changes_nothing(
    fake_redis, headers_for
):
    body = _lithic_body("PENDING")
    card = _card_row("card_token_abc")
    result, db = await _deliver("lithic", _LITHIC_SECRET, body, headers_for(body), card)
    assert result is None
    db.commit.assert_not_called()
    assert card.status == "created"
    assert fake_redis.store == {}, "no dedup claim before verification"


@pytest.mark.asyncio
async def test_card_webhook_dedups_replayed_event(fake_redis):
    """Lithic's retry of one delivery (same `webhook-id`) must not re-run the
    settlement; without this a retry mints a second rebate."""
    body = _lithic_body("SETTLED")
    headers = _lithic_headers(body, msg_id="evt_dup_check")

    card_first = _card_row("card_token_abc")
    card_first.status = "charged"  # settling assumes the auth already landed
    card_first.amount_charged = 5000
    _result, db_1 = await _deliver("lithic", _LITHIC_SECRET, body, headers, card_first)
    assert card_first.status == "completed"
    db_1.commit.assert_called()

    card_second = _card_row("card_token_abc")
    card_second.status = "charged"
    card_second.amount_charged = 5000
    _result, db_2 = await _deliver("lithic", _LITHIC_SECRET, body, headers, card_second)
    assert card_second.status == "charged", "a retried delivery must not advance the card"
    db_2.commit.assert_not_called()


@pytest.mark.asyncio
async def test_lithic_two_updates_of_one_transaction_are_not_deduped_as_one(fake_redis):
    """The transaction `token` is shared by its PENDING and SETTLED updates;
    keyed on the delivery id, the settlement is not mistaken for a replay."""
    card = _card_row("card_token_abc")
    pending = _lithic_body("PENDING")
    await _deliver("lithic", _LITHIC_SECRET, pending, _lithic_headers(pending, msg_id="e1"), card)
    assert card.status == "charged"
    settled = _lithic_body("SETTLED")
    await _deliver("lithic", _LITHIC_SECRET, settled, _lithic_headers(settled, msg_id="e2"), card)
    assert card.status == "completed"


@pytest.mark.asyncio
async def test_lithic_undocumented_type_key_is_no_stage(fake_redis):
    """Lithic names the event in `event_type`; a body using `type` instead is
    signed and valid but reports no stage (decisions §555)."""
    payload = json.loads(_lithic_body("PENDING"))
    payload["type"] = payload.pop("event_type")
    body = json.dumps(payload).encode()
    card = _card_row("card_token_abc")
    await _deliver("lithic", _LITHIC_SECRET, body, _lithic_headers(body), card)
    assert card.status == "created"


# --- Nium through the handler -----------------------------------------------


@pytest.mark.asyncio
async def test_nium_partner_key_delivery_charges_the_card_and_dedupes_on_request_id(
    fake_redis,
):
    body = _nium_body("CARD_POS_APPROVED_WEBHOOK")
    card = _card_row("card_token_abc")
    result, db = await _deliver("nium", _NIUM_PARTNER_KEY, body, _nium_headers(), card)
    assert result is None
    assert card.status == "charged"
    db.commit.assert_called()
    assert f"{DEDUP_PREFIX}nium:123e4567-e89b-12d3-a456-426655440000" in fake_redis.store


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"x-request-id": "r1"}, id="no-partner-key"),
        pytest.param(_nium_headers(key="wrong-key"), id="wrong-partner-key"),
        pytest.param({"x-partner-key": _NIUM_PARTNER_KEY}, id="no-request-id"),
        pytest.param(
            {"x-request-id": "r1", "Webhook-Signature": _sign(_NIUM_PARTNER_KEY, _nium_body())},
            id="old-hex-hmac-scheme",
        ),
    ],
)
@pytest.mark.asyncio
async def test_nium_unverifiable_delivery_is_a_silent_204_and_changes_nothing(fake_redis, headers):
    card = _card_row("card_token_abc")
    result, db = await _deliver("nium", _NIUM_PARTNER_KEY, _nium_body(), headers, card)
    assert result is None
    db.commit.assert_not_called()
    assert card.status == "created"
    assert fake_redis.store == {}


@pytest.mark.asyncio
async def test_nium_redelivery_with_the_same_request_id_is_deduped(fake_redis):
    body = _nium_body("CARD_SETTLEMENT_DEBIT_WEBHOOK")
    first = _card_row("card_token_abc")
    first.status, first.amount_charged = "charged", 50
    await _deliver("nium", _NIUM_PARTNER_KEY, body, _nium_headers(request_id="r-9"), first)
    assert first.status == "completed"
    second = _card_row("card_token_abc")
    second.status, second.amount_charged = "charged", 50
    _r, db = await _deliver(
        "nium", _NIUM_PARTNER_KEY, body, _nium_headers(request_id="r-9"), second
    )
    assert second.status == "charged"
    db.commit.assert_not_called()


@pytest.mark.parametrize(
    "event_type",
    [
        "authorization.approved",
        "transaction.settled",
        "authorization.reversal",
        "authorization.expired",
        "authorization.declined",
    ],
)
@pytest.mark.asyncio
async def test_nium_undocumented_lower_case_names_change_nothing(fake_redis, event_type):
    """§549 kept these unverified; Nium's docs show none of them, so a signed
    delivery naming one (in `eventType` or `template`) is inert (§555)."""
    for key in ("eventType", "template"):
        payload = json.loads(_nium_body())
        payload.pop("template")
        payload[key] = event_type
        card = _card_row("card_token_abc")
        card.status = "charged"
        card.amount_charged = 50
        await _deliver(
            "nium",
            _NIUM_PARTNER_KEY,
            json.dumps(payload).encode(),
            _nium_headers(request_id=f"{key}-{event_type}"),
            card,
        )
        assert card.status == "charged" and card.amount_charged == 50


@pytest.mark.asyncio
async def test_card_webhook_releases_dedup_claim_when_commit_fails(fake_redis):
    """The lost-event guard: if the DB commit rolls back AFTER the Redis
    dedup claim, the claim must be RELEASED so the provider's retry can
    reprocess. Otherwise the event is stranded as "processed" for the full
    TTL and the rebate / charge is dropped forever."""

    from app.api.cards import card_webhook
    from app.services.webhook_security import is_event_already_processed

    secret = _LITHIC_SECRET
    org = _org_with_card_secret(secret)
    card = _card_row("card_token_abc")

    body_bytes = _lithic_body("PENDING")
    headers = _lithic_headers(body_bytes, msg_id="evt_commit_fail")

    tenant_factory, db = _fake_tenant_session_factory(card)
    # The commit fails (deadlock / pool exhaustion) — the handler must roll
    # back AND release the dedup claim.
    db.commit = AsyncMock(side_effect=RuntimeError("db overloaded"))

    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        result = await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, headers),
        )

    assert result is None  # still a silent 204
    db.rollback.assert_called()
    # The claim was released → a retry is NOT deduped (returns False = "first").
    assert await is_event_already_processed("lithic", "evt_commit_fail") is False


@pytest.mark.asyncio
async def test_card_webhook_rejects_when_secret_unconfigured(fake_redis):
    """An org that hasn't configured a webhook secret must NOT accept
    any webhook — even an "unsigned" one. Otherwise activating cards
    for a tenant before setting the secret silently makes that
    tenant's rebate flow forgeable."""

    from app.api.cards import card_webhook

    org = _org_with_card_secret("")  # blank
    card = _card_row("card_token_abc")

    body_bytes = _lithic_body("PENDING")

    tenant_factory, db = _fake_tenant_session_factory(card)
    with (
        patch("app.database.control_session_factory", _fake_ctrl_session_factory([org])),
        patch("app.database.get_tenant_engine", return_value=MagicMock()),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", return_value=tenant_factory),
    ):
        await card_webhook(
            provider="lithic",
            request=_fake_request(body_bytes, _lithic_headers(body_bytes)),
        )
    db.commit.assert_not_called()
    assert card.status == "created"

    # Nium: an empty partner key on both sides is not a match.
    nium_card = _card_row("card_token_abc")
    _r, nium_db = await _deliver("nium", "", _nium_body(), _nium_headers(key=""), nium_card)
    nium_db.commit.assert_not_called()
    assert nium_card.status == "created"


# ---------------------------------------------------------------------------
# ERP webhook integration
# ---------------------------------------------------------------------------


def _ctrl_session_for_org(org):
    """Mock the control-plane session whose first execute() returns
    `org` (or None for the unknown-tenant path)."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    ctrl_db = AsyncMock()
    ctrl_db.execute = AsyncMock(return_value=result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=ctrl_db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# The generic route answers only for the tenant's own, directly connected ERP
# (decisions §399): `erp_type` must be what this block resolves to.
GENERIC_DIRECT_ERP = {"type": "generic", "integration_method": "direct"}


def _org_with_erp_secret(secret: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"erp": {**GENERIC_DIRECT_ERP, "webhook_signing_secret": secret}},
    )


@pytest.mark.asyncio
async def test_erp_webhook_rejects_unsigned_request_silently(fake_redis):
    """ERP status webhooks can transition invoices to `paid` — a
    forged event without HMAC would let an attacker mark invoices
    paid in a tenant's books. Must reject silently."""
    import json

    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("erp-secret")
    body_bytes = json.dumps(
        {
            "tenant_slug": "acme",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_1",
            "status": "Paid",
            "event_id": "ev_1",
        }
    ).encode("utf-8")

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine") as mk_engine,
        patch("app.api.erp_webhook.async_sessionmaker") as mk_factory,
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes),
        )

    assert result is None
    # No tenant DB session opened on the rejection path.
    mk_engine.assert_not_called()
    mk_factory.assert_not_called()


@pytest.mark.asyncio
async def test_erp_webhook_rejects_wrong_signature_silently(fake_redis):
    import json

    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("real")
    body_bytes = json.dumps(
        {
            "tenant_slug": "acme",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_2",
            "status": "Paid",
            "event_id": "ev_2",
        }
    ).encode("utf-8")
    wrong_sig = _sign("attacker", body_bytes)

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine") as mk_engine,
    ):
        await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": wrong_sig}),
        )

    mk_engine.assert_not_called()


@pytest.mark.asyncio
async def test_erp_webhook_silently_returns_for_unknown_tenant(fake_redis):
    """A body that names a tenant slug we've never heard of must
    NOT distinguish itself from a bad-signature response — both 204
    silently so an attacker can't enumerate slugs."""
    import json

    from app.api.erp_webhook import erp_webhook

    body_bytes = json.dumps(
        {
            "tenant_slug": "no-such-tenant",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_3",
            "status": "Paid",
            "event_id": "ev_3",
        }
    ).encode("utf-8")

    opened = AsyncMock()
    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(None)),
        patch("app.api.erp_webhook.provider_config", opened),
    ):
        result = await erp_webhook(
            erp_type="generic",
            # Well-formed, so the request reaches the slug lookup (decisions §408).
            request=_fake_request(body_bytes, {"X-Webhook-Signature": _sign("guess", body_bytes)}),
        )

    assert result is None
    opened.assert_not_awaited()  # nothing unsealed for a slug that names no tenant


@pytest.mark.asyncio
async def test_erp_webhook_whitelists_audit_details_no_pii(fake_redis):
    """A signed ERP webhook whose `details` block carries vendor PII (bank
    account / tax id / address) must NOT splat that into the append-only audit
    row. Only the non-PII routing identifiers are recorded."""
    import json

    from app.api.erp_webhook import erp_webhook
    from app.models.invoice import InvoiceStatus

    secret = "erp-secret"
    org = _org_with_erp_secret(secret)
    body = {
        "tenant_slug": "acme",
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_9",
        "status": "Open",  # → posted_in_erp (a legal step from sent_to_erp)
        "event_id": "ev_9",
        "details": {
            "vendor_bank_account": "123456789",
            "vendor_tin": "12-3456789",
            "vendor_address": "1 Secret Lane",
        },
    }
    body_bytes = json.dumps(body).encode("utf-8")
    sig = _sign(secret, body_bytes)

    # `id` too: a real Invoice always carries one, and a non-`Paid` status for
    # a payable invoice now looks up any ERP payment kept on it (decisions §319).
    invoice = SimpleNamespace(id=uuid.uuid4(), status=InvoiceStatus.sent_to_erp)
    tenant_factory, db = _fake_tenant_session_factory(invoice)

    captured: dict = {}

    async def _capture(_db, _inv, _target, *, action_name, details):
        captured["details"] = details

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine", return_value=MagicMock()),
        patch("app.api.erp_webhook.async_sessionmaker", return_value=tenant_factory),
        patch("app.services.erp_status_report.transition_invoice", _capture),
    ):
        await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": sig}),
        )

    details = captured["details"]
    assert set(details) == {"erp_type", "erp_status", "erp_document_id", "raw_event_id"}
    # None of the ERP payload's PII leaked into the audit row.
    serialized = json.dumps(details)
    assert "123456789" not in serialized
    assert "12-3456789" not in serialized
    assert "Secret Lane" not in serialized


@pytest.mark.asyncio
async def test_erp_webhook_logs_when_signing_secret_unconfigured(fake_redis, caplog):
    """An org that never set an ERP webhook signing secret must fail closed
    (silent 204, no tenant DB touched) AND surface a PII-free config warning so
    operators learn the integration is unconfigured rather than silently
    dropping every event. The secret value is never logged (it's empty here)."""
    import json
    import logging

    from app.api.erp_webhook import erp_webhook

    org = _org_with_erp_secret("")  # unconfigured
    body_bytes = json.dumps(
        {
            "tenant_slug": "acme",
            "correlation_id": str(uuid.uuid4()),
            "erp_document_id": "doc_x",
            "status": "Paid",
            "event_id": "ev_x",
        }
    ).encode("utf-8")

    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.get_tenant_engine") as mk_engine,
        caplog.at_level(logging.WARNING, logger="app.api.erp_webhook"),
    ):
        result = await erp_webhook(
            erp_type="generic",
            request=_fake_request(body_bytes, {"X-Webhook-Signature": _sign("x", body_bytes)}),
        )

    assert result is None  # silent 204 to the caller
    mk_engine.assert_not_called()  # never opened the tenant DB
    assert any(
        "signing_secret" in r.getMessage() and "acme" in r.getMessage() for r in caplog.records
    ), "expected a PII-free unconfigured-secret warning"


# ---------------------------------------------------------------------------
# ERP webhook: nothing unsealed before the checks that need no secret (§408)
# ---------------------------------------------------------------------------


def _erp_body(**overrides) -> bytes:
    import json

    body = {
        "tenant_slug": "acme",
        "correlation_id": str(uuid.uuid4()),
        "erp_document_id": "doc_408",
        "status": "Open",
        "event_id": "ev_408",
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


async def _erp_post_counting_unseals(body: bytes, headers: dict, *, org, opened=None):
    """The generic route with ``provider_config`` — the one opening of the
    sealed ERP block — as a spy, and the tenant DB refused outright."""
    from app.api.erp_webhook import erp_webhook

    opened = opened or AsyncMock(side_effect=lambda o, *_a, **_k: o.settings["erp"])
    ctrl = _ctrl_session_for_org(org)
    with (
        patch("app.api.erp_webhook.control_session_factory", ctrl),
        patch("app.api.erp_webhook.provider_config", opened),
        patch("app.api.erp_webhook.get_tenant_engine") as engine,
    ):
        result = await erp_webhook(erp_type="generic", request=_fake_request(body, headers))
    return result, opened, ctrl, engine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signature",
    [None, "", "sig", "A" * 64, "sha256=" + "a" * 64, "a" * 63],
    ids=["absent", "empty", "short", "upper-case", "prefixed", "63-digits"],
)
async def test_erp_webhook_signature_that_could_never_verify_looks_nothing_up(
    fake_redis, signature
):
    """Only 64 lower-case hex digits can ever equal `hexdigest()`; anything
    else is refused before the tenant is even looked up."""
    body = _erp_body()
    headers = {} if signature is None else {"X-Webhook-Signature": signature}
    result, opened, ctrl, engine = await _erp_post_counting_unseals(
        body, headers, org=_org_with_erp_secret("real")
    )
    assert result is None
    ctrl.assert_not_called()
    opened.assert_not_awaited()
    engine.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "org",
    [
        None,
        SimpleNamespace(
            id=uuid.uuid4(),
            slug="acme",
            db_name="feoh_acme",
            settings={
                "erp": {
                    "type": "netsuite",
                    "integration_method": "direct",
                    "webhook_signing_secret": "real",
                }
            },
        ),
        _org_with_erp_secret(""),  # the tenant's ERP, no key stored
        SimpleNamespace(id=uuid.uuid4(), slug="acme", db_name="feoh_acme", settings={}),
    ],
    ids=["unknown-slug", "another-erp", "no-key-stored", "no-erp"],
)
async def test_erp_webhook_unseals_nothing_for_a_tenant_whose_key_could_not_verify(fake_redis, org):
    body = _erp_body()
    result, opened, _ctrl, engine = await _erp_post_counting_unseals(
        body, {"X-Webhook-Signature": _sign("guess", body)}, org=org
    )
    assert result is None
    opened.assert_not_awaited()
    engine.assert_not_called()


@pytest.mark.asyncio
async def test_erp_webhook_unknown_slug_answers_exactly_as_a_bad_signature(fake_redis):
    body = _erp_body()
    headers = {"X-Webhook-Signature": _sign("not-the-tenants-key", body)}
    unknown, opened_unknown, _, _ = await _erp_post_counting_unseals(body, headers, org=None)
    refused, opened_real, _, engine = await _erp_post_counting_unseals(
        body, headers, org=_org_with_erp_secret("real")
    )
    assert unknown is None and refused is None
    opened_unknown.assert_not_awaited()
    opened_real.assert_awaited_once()  # the residual: the real key is opened to check
    engine.assert_not_called()


@pytest.mark.asyncio
async def test_erp_webhook_a_source_cycling_tenants_is_cut_off(fake_redis, monkeypatch):
    """The generic route shares the Merge route's unseal budget (decisions
    §525): once an address has failed against the budgeted number of distinct
    tenants, the next tenant's key is not opened and the answer is the same
    silent 204; a verified delivery before that spent nothing."""
    from app.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "erp_webhook_unverified_tenants_per_source_limit", 2)
    body = _erp_body(correlation_id="not-a-uuid")  # verified, then a silent 204

    async def _from_one_address(org, secret):
        from app.api.erp_webhook import erp_webhook

        opened = AsyncMock(side_effect=lambda o, *_a, **_k: o.settings["erp"])
        req = _fake_request(body, {"X-Webhook-Signature": _sign(secret, body)})
        req.client.host = "198.51.100.7"
        with (
            patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
            patch("app.api.erp_webhook.provider_config", opened),
            patch("app.api.erp_webhook.get_tenant_engine") as engine,
        ):
            result = await erp_webhook(erp_type="generic", request=req)
        engine.assert_not_called()
        return result, opened.await_count

    for _ in range(3):
        assert await _from_one_address(_org_with_erp_secret("real"), "real") == (None, 1)
    for expected in (1, 1, 0):
        assert await _from_one_address(_org_with_erp_secret("real"), "guess") == (None, expected)


@pytest.fixture
def erp_unseal_budget(monkeypatch):
    """The limiter on, with a small budget: 2 distinct tenants per source."""
    from app.config import settings

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "erp_webhook_unverified_tenants_per_source_limit", 2)
    return settings


async def _erp_from(source: str, org, signature: str, body: bytes, opened=None):
    """One generic-route delivery from ``source`` with the unseal as a spy;
    returns (result, opened)."""
    from app.api.erp_webhook import erp_webhook

    opened = opened or AsyncMock(side_effect=lambda o, *_a, **_k: o.settings["erp"])
    req = _fake_request(body, {"X-Webhook-Signature": signature})
    req.client.host = source
    with (
        patch("app.api.erp_webhook.control_session_factory", _ctrl_session_for_org(org)),
        patch("app.api.erp_webhook.provider_config", opened),
        patch("app.api.erp_webhook.get_tenant_engine") as engine,
    ):
        result = await erp_webhook(erp_type="generic", request=req)
    engine.assert_not_called()
    return result, opened


@pytest.mark.asyncio
async def test_erp_webhook_a_wrong_signature_spends_the_source_budget(
    fake_redis, erp_unseal_budget
):
    """Parity with the Merge route (§525): a wrong signature at the generic
    route spends the source's budget, per DISTINCT tenant — repeats against
    one tenant are one entry — and the third tenant is refused unopened with
    the same silent 204, while another address still opens it."""
    body = _erp_body()
    wrong = _sign("attacker", body)
    acme, globex, initech = (_org_with_erp_secret("real") for _ in range(3))

    for _ in range(4):
        result, opened = await _erp_from("198.51.100.7", acme, wrong, body)
        assert result is None
        opened.assert_awaited_once()
    result, opened = await _erp_from("198.51.100.7", globex, wrong, body)
    opened.assert_awaited_once()

    result, opened = await _erp_from("198.51.100.7", initech, wrong, body)
    assert result is None
    opened.assert_not_awaited()

    _result, opened = await _erp_from("192.0.2.44", initech, wrong, body)
    opened.assert_awaited_once()


@pytest.mark.asyncio
async def test_erp_webhook_a_key_that_cannot_be_opened_spends_the_budget_too(
    fake_redis, erp_unseal_budget
):
    """A failed opening still cost the KMS call the budget bounds (parity
    with the Merge route)."""
    from app.services.credential_crypto import CredentialCryptoError

    body = _erp_body()
    failing = AsyncMock(side_effect=CredentialCryptoError("kms"))
    for _ in range(3):
        result, _opened = await _erp_from(
            "198.51.100.7", _org_with_erp_secret("real"), _sign("real", body), body, failing
        )
        assert result is None
    assert failing.await_count == 2  # the third tenant was refused before opening


@pytest.mark.asyncio
async def test_erp_webhook_a_spent_budget_still_lets_a_cache_served_tenant_verify(
    fake_redis, erp_unseal_budget, monkeypatch
):
    """Parity with the Merge route (decisions §527): once the source has spent
    its budget, a tenant whose data key the DEK cache holds is opened and
    verified (no KMS call to bound), a cold one is refused unopened, and the
    warm one is refused too once its cache entry is past the TTL."""
    import time

    from app.services import credential_crypto, provider_credentials

    body = _erp_body(correlation_id="not-a-uuid")  # verified, then a silent 204
    for _ in range(2):
        await _erp_from("198.51.100.7", _org_with_erp_secret("real"), _sign("x", body), body)

    warm, cold = _org_with_erp_secret("real"), _org_with_erp_secret("real")
    for org in (warm, cold):
        org.sealed_envelopes = {
            "erp": credential_crypto.Envelope(
                ciphertext=b"", wrapped_key=uuid.uuid4().bytes, key_provider="kms", key_id="k"
            )
        }
    credential_crypto.clear_dek_cache()
    key = credential_crypto._dek_cache_key(
        warm.sealed_envelopes["erp"].wrapped_key, provider_credentials._context(warm.id, "erp")
    )
    now = time.monotonic()
    credential_crypto._dek_cache[key] = (now + 60.0, b"\x00" * 32)
    try:
        result, opened = await _erp_from("198.51.100.7", warm, _sign("real", body), body)
        assert result is None
        opened.assert_awaited_once()
        result, opened = await _erp_from("198.51.100.7", cold, _sign("real", body), body)
        assert result is None
        opened.assert_not_awaited()

        monkeypatch.setattr(credential_crypto.time, "monotonic", lambda: now + 61.0)
        result, opened = await _erp_from("198.51.100.7", warm, _sign("real", body), body)
        opened.assert_not_awaited()
    finally:
        credential_crypto.clear_dek_cache()


@pytest.mark.asyncio
async def test_erp_webhook_credentials_that_cannot_be_opened_are_a_silent_204(fake_redis):
    """Not a 503: the signature cannot be checked without the key, and a 503
    before it verifies would answer a configured slug differently (§408)."""
    from app.services.credential_crypto import CredentialCryptoError

    body = _erp_body()
    result, _opened, _ctrl, engine = await _erp_post_counting_unseals(
        body,
        {"X-Webhook-Signature": _sign("real", body)},
        org=_org_with_erp_secret("real"),
        opened=AsyncMock(side_effect=CredentialCryptoError("kms")),
    )
    assert result is None
    engine.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", [123, ["acme"], {"slug": "acme"}])
async def test_erp_webhook_non_string_tenant_slug_is_a_silent_204(fake_redis, slug):
    """Used to reach the slug query as a non-string bind (a 500)."""
    body = _erp_body(tenant_slug=slug)
    result, opened, ctrl, _engine = await _erp_post_counting_unseals(
        body, {"X-Webhook-Signature": _sign("real", body)}, org=_org_with_erp_secret("real")
    )
    assert result is None
    ctrl.assert_not_called()
    opened.assert_not_awaited()


@pytest.mark.asyncio
async def test_erp_webhook_verified_body_with_a_non_uuid_correlation_id_is_a_silent_204(
    fake_redis,
):
    """It names no invoice of ours: a decision, not our failure. It used to
    raise inside the handler's try into the 503 path, so the ERP redelivered
    the same unparseable body for ever."""
    body = _erp_body(correlation_id="not-a-uuid")
    result, opened, _ctrl, engine = await _erp_post_counting_unseals(
        body, {"X-Webhook-Signature": _sign("real", body)}, org=_org_with_erp_secret("real")
    )
    assert result is None
    opened.assert_awaited_once()
    engine.assert_not_called()


@pytest.mark.asyncio
async def test_erp_webhook_dedup_store_down_after_verifying_asks_for_redelivery():
    """Our failure on a verified delivery: a bodyless 503 (it escaped as a 500)."""
    body = _erp_body()
    with patch(
        "app.api.erp_webhook.is_event_already_processed",
        AsyncMock(side_effect=ConnectionError("redis")),
    ):
        result, _opened, _ctrl, engine = await _erp_post_counting_unseals(
            body, {"X-Webhook-Signature": _sign("real", body)}, org=_org_with_erp_secret("real")
        )
    assert result.status_code == 503 and result.body == b""
    engine.assert_not_called()


@pytest.mark.parametrize(
    ("provided", "ok"),
    [
        ("a" * 64, True),
        ("0123456789abcdef" * 4, True),
        ("a" * 63, False),
        ("a" * 65, False),
        ("A" * 64, False),  # the verifier compares against hexdigest(), lower-case only
        ("g" * 64, False),
        ("a" * 63 + "\n", False),
        ("", False),
        (None, False),
    ],
)
def test_well_formed_hex_sha256_accepts_only_what_the_verifier_can(provided, ok):
    from app.services.webhook_security import well_formed_hex_sha256

    assert well_formed_hex_sha256(provided) is ok
