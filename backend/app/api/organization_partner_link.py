"""Organization settings → consent to be adopted by a partner (decisions §14, §574).

A workspace's admin mints a short-lived, single-use, HMAC-signed *link code*
for their OWN org and hands it to a partner; the partner's admin redeems it at
``POST /api/partner/children``. Handing the code over is the consent: without
it no partner can attach this workspace.

Consenting to BE adopted is not a partner act, so this lives in Organization
settings and needs only the admin role — not the operator-granted partner
capability (``Organization.is_partner``) every ``/api/partner`` route requires.
It used to be ``POST /api/partner/link-code``; it moved when that surface was
gated on the capability, so a standalone workspace's admin can still consent.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import ROLE_ADMIN, require_roles
from app.config import settings
from app.models.organization import Organization
from app.models.user import User
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.partner_link_token import build_link_code
from app.tenant import get_tenant

router = APIRouter(prefix="/organization", tags=["organization"])


class LinkCodeResponse(BaseModel):
    """The minted link code an org's admin hands to a prospective partner.

    PII-free: the code is an opaque signed token over the child org id only (no
    name/slug), plus a human-readable expiry hint so the issuer knows how long
    the partner has to redeem it.
    """

    link_code: str
    expires_in_minutes: int


@router.post("/partner-link-code", response_model=LinkCodeResponse)
async def mint_partner_link_code(
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(get_tenant),
) -> LinkCodeResponse:
    """Mint a single-use link code so THIS org can be attached as a child.

    Admin-only, partner or not; the code is issued FOR the caller's own org
    (``org.id`` from the ``get_tenant`` chokepoint — a swapped header / forged
    Host can't widen it). The code carries only the caller's org id under an
    HMAC signature; it expires in ``FEOH_PARTNER_LINK_TTL_MINUTES`` and is
    burned on first redeem.

    A 503 (feature off) when no signing key is configured — distinct from a 4xx
    so the operator knows to set ``FEOH_PARTNER_LINK_SIGNING_KEY``, not that the
    admin did something wrong. Issuing is audited (PII-free) into the org's own
    trail as ``partner.link_code_issued``, as it always was.
    """
    code = build_link_code(
        child_org_id=org.id,
        signing_key=settings.partner_link_signing_key,
        ttl_minutes=settings.partner_link_ttl_minutes,
    )
    if code is None:
        # No key → fail closed: the feature is not configured.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Partner linking is not enabled.",
        )

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="partner.link_code_issued",
        entity_type="organization",
        entity_id=org.id,
        details={"ttl_minutes": settings.partner_link_ttl_minutes},
    )
    return LinkCodeResponse(
        link_code=code,
        expires_in_minutes=settings.partner_link_ttl_minutes,
    )
