"""Self-service "email me my workspace address" (decisions §518).

``POST /api/workspace-lookup {email}`` — public by design: the caller is, by
definition, someone who cannot find the page they sign in on. It answers every
address the same way ("if that address has an account, we've emailed it"), and
the ONLY party it ever tells which workspace an address belongs to is the
mailbox itself, by email.

**No existence oracle.** The route never reads the ``users`` table. It applies
the abuse controls every address shares — the per-IP budget, the address-shape
check, the captcha, the per-address budget — then hands the address to a
detached task (`utils/detached_tasks.spawn_detached`, the shape
`/auth/forgot-password` uses) and returns. The lookup, the org read, the send
and the audit row all happen in that task, after the response is on its way,
so neither the body, the status nor the response time depends on whether the
address has an account.

**Abuse controls are signup's, not a copy of them.** The same
``check_rate_limit`` (per IP, then per address — the address keyed through
``auth_identity_key`` so it never becomes part of a Redis key) and the same
``verify_captcha`` as `/signup/start`, on their own budgets
(``FEOH_WORKSPACE_LOOKUP_RATE_LIMIT_PER_HOUR`` /
``FEOH_WORKSPACE_LOOKUP_EMAIL_RATE_LIMIT_PER_HOUR``) so one flow cannot spend
the other's.

**Only a solved captcha spends an address's budget** (decisions §553). The
per-IP budget comes first (cheap, stops a flood from one source), then the
captcha, then the per-address budget. With the address budget first, anyone
could exhaust a stranger's hourly lookups with unsolved requests from rotating
IPs and lock that person out of the flow.

Not behind ``FEOH_SIGNUP_ENABLED``: closing signup on an invite-only deployment
does not stop its existing users forgetting their workspace. So the captcha is
required on every deployed environment, signup open or not
(``config.py::_require_captcha_in_deployed_envs``, §553).

The address is never logged: failures are logged by user id and exception class.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.refusals import coded_refusal
from app.api.signup import SIGNUP_EMAIL_INVALID
from app.config import settings
from app.database import control_session_factory
from app.models.organization import Organization
from app.models.user import User
from app.schemas.workspace_lookup import WorkspaceLookupRequest, WorkspaceLookupResponse
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.branding import get_brand_context
from app.services.email_adapters import (
    EmailMessage,
    get_email_adapter,
    normalize_locale,
    translate,
)
from app.services.email_adapters.layout import (
    email_button,
    email_fallback_link,
    email_note,
    email_paragraph,
)
from app.services.rate_limit import auth_identity_key, check_rate_limit, resolve_client_ip
from app.utils.detached_tasks import spawn_detached
from app.utils.emails import email_matches, exact_email_first, looks_like_email
from app.utils.hcaptcha import CaptchaError, verify_captcha
from app.utils.tenant_urls import sign_in_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspace-lookup", tags=["signup"])

#: The audit action written to the account's own tenant when its workspace
#: address is emailed — who asked (the IP) is the only detail; the address is
#: the account's own.
AUDIT_ACTION = "auth.workspace_lookup.emailed"


@router.post("", response_model=WorkspaceLookupResponse)
async def workspace_lookup(body: WorkspaceLookupRequest, request: Request):
    # 1. Per-IP budget before anything else, like `/signup/start`.
    await check_rate_limit(
        "workspace_lookup",
        request,
        limit=settings.workspace_lookup_rate_limit_per_hour,
        window_seconds=3600,
    )

    # 2. Shape only. Says nothing about accounts: it is the same answer for
    #    every string that is not an address. Padding is dropped first, as
    #    sign-in drops it (a pasted address often carries a space).
    address = body.email.strip()
    if not looks_like_email(address):
        raise HTTPException(
            status_code=422,
            detail=coded_refusal(
                SIGNUP_EMAIL_INVALID, "Enter a valid email address.", field="email"
            ),
        )

    # 3. Captcha (skipped only when no secret is configured — local dev; a
    #    deployed environment refuses to boot without one, §553). Before the
    #    per-address budget, so an unsolved request cannot spend it.
    client_ip = resolve_client_ip(request)
    try:
        await verify_captcha(body.captcha_token, client_ip)
    except CaptchaError as exc:
        raise HTTPException(status_code=400, detail=coded_refusal(exc.code, str(exc))) from exc

    # 4. Per-ADDRESS budget, so an attacker rotating IPs cannot fill one
    #    person's inbox. Checked before any lookup, so a known and an unknown
    #    address spend and exhaust it identically.
    await check_rate_limit(
        "workspace_lookup_email",
        subject=auth_identity_key(address),
        limit=settings.workspace_lookup_email_rate_limit_per_hour,
        window_seconds=3600,
    )

    # 5. Everything that depends on whether the address has an account runs
    #    after the response. Spawned for EVERY address that got this far.
    spawn_detached(
        _email_workspace_address(address, body.locale, client_ip),
        name="workspace_lookup",
    )
    return WorkspaceLookupResponse()


async def _email_workspace_address(address: str, locale: str | None, ip: str | None) -> None:
    """Find the address's account and email it its workspace's sign-in page.

    Detached: its own control-plane session (the request's is gone), and
    nothing it does reaches the response. No account, a deactivated one, or a
    workspace with no sign-in URL configured all end silently.
    """
    async with control_session_factory() as db:
        user = (
            await db.execute(
                select(User)
                .where(email_matches(User.email, address))
                .order_by(exact_email_first(User.email, address).desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if user is None or not user.is_active:
            return
        org = await db.get(Organization, user.organization_id)
        if org is None:
            return
        user_id: uuid.UUID = user.id
        to = user.email
        full_name = user.full_name
        email_locale = normalize_locale(user.locale or locale)
        workspace_name = org.name
        # Where the account actually signs in: a workspace on its own domain
        # signs in there (the wrong-workspace refusal's choice, decisions §288).
        url = sign_in_url(org.slug, org.settings, email=to, prefer_custom_domain=True)
        org_id = org.id

    if url is None:
        logger.warning(
            "Workspace lookup matched an account but no sign-in URL is configured "
            "(org=%s) — no email sent",
            org_id,
        )
        return
    try:
        await get_email_adapter().send(
            workspace_address_message(
                to=to, full_name=full_name, workspace=workspace_name, url=url, locale=email_locale
            )
        )
    except Exception as exc:  # noqa: BLE001
        # The class only: an SMTP refusal's message can quote the recipient.
        logger.error("Workspace lookup email failed (user=%s): %s", user_id, type(exc).__name__)
        return
    await dispatch_auth_audit(
        organization_id=org_id,
        actor_id=None,
        action=AUDIT_ACTION,
        entity_id=user_id,
        details={"ip": ip},
    )


def workspace_address_message(
    *, to: str, full_name: str, workspace: str, url: str, locale: str
) -> EmailMessage:
    """The email: whose workspace, its sign-in link, and an "ignore if not you"."""
    greeting = translate("workspace_lookup.greeting", locale, name=full_name)
    lead = translate("workspace_lookup.body", locale, workspace=workspace)
    note = translate("workspace_lookup.ignore", locale)
    # Every layout block escapes what it is given: the name and workspace name
    # are stored user input.
    html = (
        email_paragraph(greeting)
        + email_paragraph(lead)
        + email_button(url, translate("workspace_lookup.cta", locale), get_brand_context(None))
        + email_note(note)
        + email_fallback_link(translate("email.layout.fallback_link", locale), url)
    )
    return EmailMessage(
        to=to,
        subject=translate("workspace_lookup.subject", locale),
        body_text=f"{greeting}\n\n{lead}\n\n{url}\n\n{note}\n",
        body_html=html,
        locale=locale,
        preheader=lead,
    )
