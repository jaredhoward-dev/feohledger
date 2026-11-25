"""Shared webhook-security primitives.

Project invariant #9 says every inbound webhook must
  - verify the provider's HMAC over the raw bytes, and
  - dedupe by event id so a re-delivery doesn't replay a one-time
    effect (a settled payment, a card rebate, an ERP status flip).

The payment adapter implements its own HMAC verification inline. This
module exposes the same primitives in a form the card- and ERP-webhook
handlers (which don't go through the adapter abstraction) can reuse: a body
HMAC (hex / base64 / base64url), the Standard-Webhooks scheme Lithic signs
with (`verify_standard_webhook`), and the static shared header Nium sends in
place of a signature (`verify_shared_header_token`, decisions §555).

Dedup uses Redis with a TTL so retries within the provider's
re-delivery window are caught, but the key set doesn't grow without
bound. The 72-hour default covers the widest processor retry window we
integrate with (Lithic retries a failed delivery on an exponential
backoff over ~3 days); a shorter TTL would let a late redelivery slip
past the dedup and replay the one-time effect.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re

from app.redis import get_redis

logger = logging.getLogger(__name__)

DEDUP_PREFIX = "webhook:event:"
# 3 days — must cover the LONGEST provider retry window (Lithic ~72h), or a
# redelivery after the key expires reprocesses the event (double rebate).
DEFAULT_DEDUP_TTL_SECONDS = 72 * 60 * 60


def verify_hmac_sha256(secret: str, raw_body: bytes, provided_hex: str | None) -> bool:
    """Constant-time HMAC-SHA256 check.

    Returns False (not raises) so the caller can decide whether to
    log the rejection silently or return an error. Empty / missing
    secret or missing signature always fail — there's no
    "no secret = anyone can post" mode.
    """
    if not secret or not provided_hex:
        return False
    try:
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    except Exception:  # noqa: BLE001 — defensive: malformed input must fail closed
        return False
    return hmac.compare_digest(expected, provided_hex)


def verify_hmac_sha256_base64(secret: str, raw_body: bytes, provided_b64: str | None) -> bool:
    """:func:`verify_hmac_sha256` for a provider that sends the digest base64
    encoded (Intuit's ``intuit-signature``) rather than hex.

    Compares the raw digests, so padding or a re-encoding of the signature
    cannot make two different digests compare equal. Same fail-closed rules:
    an empty secret or signature, or one that is not base64, is False.
    """
    import base64
    import binascii

    if not secret or not provided_b64:
        return False
    try:
        provided = base64.b64decode(provided_b64.strip(), validate=True)
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    except (binascii.Error, ValueError):
        return False
    return hmac.compare_digest(expected, provided)


_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


def _decode_base64url(provided: str) -> bytes | None:
    """The bytes a strict base64url signature encodes, or None.

    Only the URL-safe alphabet (a standard-base64 ``+`` / ``/`` is refused),
    padding optional."""
    text = provided.strip()
    if not _BASE64URL.fullmatch(text):
        return None
    bare = text.rstrip("=")
    try:
        return base64.urlsafe_b64decode(bare + "=" * (-len(bare) % 4))
    except (binascii.Error, ValueError):
        return None


def well_formed_hex_sha256(provided: str | None) -> bool:
    """Could ``provided`` be a :func:`verify_hmac_sha256` signature at all?

    A shape check, no secret involved: 64 lower-case hex digits, the only form
    that verifier can ever accept (it compares against ``hexdigest()``
    exactly). A per-tenant webhook route asks this BEFORE it opens the tenant's
    sealed key, so a request that could never verify costs no unseal
    (decisions §408)."""
    return bool(provided) and _HEX_SHA256.fullmatch(provided) is not None


def well_formed_base64url_sha256(provided: str | None) -> bool:
    """:func:`well_formed_hex_sha256` for :func:`verify_hmac_sha256_base64url`:
    strict base64url that decodes to exactly a SHA-256 digest (32 bytes)."""
    if not provided:
        return False
    decoded = _decode_base64url(provided)
    return decoded is not None and len(decoded) == hashlib.sha256().digest_size


def verify_hmac_sha256_base64url(secret: str, raw_body: bytes, provided_b64url: str | None) -> bool:
    """:func:`verify_hmac_sha256` for a provider that sends the digest
    base64url encoded (Merge.dev's ``X-Merge-Webhook-Signature``).

    Strict: only the URL-safe alphabet is accepted (a standard-base64 ``+`` /
    ``/`` is not), padding is optional, and the raw digests are compared, so
    no re-encoding of the signature makes two different digests equal. Same
    fail-closed rules: an empty secret or signature, or one that does not
    decode, is False.
    """
    if not secret or not provided_b64url:
        return False
    provided = _decode_base64url(provided_b64url)
    if provided is None:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, provided)


#: Lithic's reference verifier rejects a ``webhook-timestamp`` more than five
#: minutes from now, either way (https://docs.lithic.com/docs/events-api,
#: checked 2026-11-25). The signed timestamp is the replay protection: a
#: captured delivery stops verifying once it is older than this.
STANDARD_WEBHOOK_TOLERANCE_SECONDS = 5 * 60
_STANDARD_WEBHOOK_SECRET_PREFIX = "whsec_"


def verify_standard_webhook(
    secret: str,
    msg_id: str | None,
    timestamp: str | None,
    raw_body: bytes,
    signature_header: str | None,
    *,
    tolerance_seconds: int = STANDARD_WEBHOOK_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Verify a Standard-Webhooks / Svix-style delivery (Lithic's scheme).

    https://docs.lithic.com/docs/events-api (checked 2026-11-25): the headers
    are ``webhook-id`` / ``webhook-timestamp`` / ``webhook-signature``; the
    signed content is ``"{id}.{timestamp}.{raw body}"``; the key is the
    base64 part of a ``whsec_``-prefixed secret; the header is a
    space-separated list of ``v1,<base64 HMAC-SHA256>`` (several during a key
    rotation, any one of which may match).

    Fail closed: a missing header, a secret without the ``whsec_`` prefix or
    whose key is not base64, a non-integer timestamp, one outside
    ``tolerance_seconds`` of ``now`` (either direction), or no matching ``v1``
    signature is False. Digests are compared in constant time.
    """
    import time

    if not secret or not msg_id or not timestamp or not signature_header:
        return False
    if not secret.startswith(_STANDARD_WEBHOOK_SECRET_PREFIX):
        return False
    try:
        key = base64.b64decode(secret[len(_STANDARD_WEBHOOK_SECRET_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        return False
    if not key:
        return False
    ts_text = timestamp.strip()
    if not ts_text.isdigit():
        return False
    current = time.time() if now is None else now
    if abs(current - int(ts_text)) > tolerance_seconds:
        return False
    signed = msg_id.encode("utf-8") + b"." + ts_text.encode("ascii") + b"." + raw_body
    expected = hmac.new(key, signed, hashlib.sha256).digest()
    matched = False
    for candidate in signature_header.split():
        version, _, encoded = candidate.partition(",")
        if version != "v1" or not encoded:
            continue
        try:
            provided = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        # No early exit: every listed signature is compared.
        matched = hmac.compare_digest(expected, provided) or matched
    return matched


def sign_standard_webhook(secret: str, msg_id: str, timestamp: int, raw_body: bytes) -> str:
    """The ``webhook-signature`` value :func:`verify_standard_webhook` accepts
    (``v1,<base64>``). For tests and local tooling only — a provider signs its
    own deliveries."""
    key = base64.b64decode(secret[len(_STANDARD_WEBHOOK_SECRET_PREFIX) :])
    signed = f"{msg_id}.{timestamp}.".encode() + raw_body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def verify_shared_header_token(expected: str, provided: str | None) -> bool:
    """Constant-time check of a static shared value a provider sends in a
    header on every delivery (Nium's client-configured ``x-partner-key``,
    https://docs.nium.com/docs/developers/notifications-and-webhooks, checked
    2026-11-25 — Nium documents no body signature).

    Weaker than an HMAC: it authenticates the sender but does not bind the
    body and has no timestamp, so dedupe by the provider's event id is the
    only replay control. Empty on either side is False.
    """
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


async def is_event_already_processed(
    provider: str, event_id: str, *, ttl_seconds: int = DEFAULT_DEDUP_TTL_SECONDS
) -> bool:
    """Record an event id (provider + event) and return True if it
    was already recorded within the TTL window.

    The set-if-not-exists semantics mean callers can do::

        if await is_event_already_processed("lithic", evt.id):
            return  # silent ack, same response as first delivery

    A `False` return means the caller is the *first* to claim this
    event id — they must drive the side effect now.
    """
    if not event_id:
        # Empty / missing event id can't be deduped — treat as "always
        # first delivery" but log so an operator can spot a provider
        # that's stopped including event ids.
        logger.info("[webhook-dedup] missing event id for provider=%s", provider)
        return False
    r = await get_redis()
    key = f"{DEDUP_PREFIX}{provider}:{event_id}"
    # SET key value NX EX <ttl> — returns OK if set, nil if key already
    # existed. redis-py's `set` returns True / None for that.
    was_set = await r.set(key, "1", nx=True, ex=ttl_seconds)
    return not was_set


async def release_event_claim(provider: str, event_id: str) -> None:
    """Release a dedup claim made by `is_event_already_processed`.

    A Redis `SET NX` claim is durable the instant it's written — but the side
    effect it guards (a rebate mint, a charge transition) isn't durable until
    the DB transaction commits. If that commit rolls back AFTER the claim, the
    event id would stay claimed for the full TTL, so the provider's retry is
    silently deduped and the one-time effect is lost forever. Callers must
    release the claim on any post-claim failure so the retry can reprocess.

    Best-effort: an empty event id is a no-op (nothing was claimed), and a Redis
    hiccup here just falls back to TTL expiry rather than raising.
    """
    if not event_id:
        return
    try:
        r = await get_redis()
        await r.delete(f"{DEDUP_PREFIX}{provider}:{event_id}")
    except Exception:  # noqa: BLE001 — release is best-effort; never raise
        logger.warning("[webhook-dedup] failed to release claim for provider=%s", provider)


def extract_signature_header(headers: dict, *candidates: str) -> str | None:
    """Pull the first present signature header out of a request.

    Different providers use different header names (Lithic:
    `webhook-signature`, Stripe-style: `Stripe-Signature`,
    generic: `X-Signature`). Headers are case-insensitive in HTTP,
    so we walk the dict comparing lower-cased names.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None
