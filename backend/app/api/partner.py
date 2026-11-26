"""Partner / reseller multi-tenant admin (white-label).

A *partner* (reseller) org administers a set of branded *child* tenants. The
relationship is the nullable self-FK ``Organization.parent_org_id`` (migration
0065, control plane): a child points at its parent. WHICH orgs may act as a
partner is the operator-granted ``Organization.is_partner`` (migration 0151,
``docs/decisions.md`` §574): every route here requires it on top of the admin
role, and refuses a workspace without it with a coded 403
(``partner_capability_required``). The operator grants it with
``scripts/set_partner_capability.py``; nothing in the API writes it.

Consenting to BE adopted is not a partner act, so minting the link code lives
in Organization settings (``POST /api/organization/partner-link-code``, any
admin, no capability) and only the redeem side lives here.

Trust model (the load-bearing point): the caller's org is resolved by the
standard ``get_tenant`` chokepoint, which cross-checks the JWT ``org`` claim
against the resolved tenant — so a partner admin authenticates as, and can only
act as, *their own* partner org. EVERY child query in this module is then
scoped ``Organization.parent_org_id == partner.id``: a partner can never see or
mutate an org it didn't parent. Looking up a non-child org id yields the same
opaque 404 as a missing one (no cross-tenant enumeration).

Each child's branding write lands on the CHILD org's control-plane row
(``settings.brand`` JSONB) and is audited into the CHILD's tenant trail —
exactly the same write + audit path as the child's own
``PUT /api/organization/branding``, just initiated by the parent. PII-free
audit (booleans only, never raw values). Admin-only on every mutation.
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, require_roles
from app.api.organization import _resolve_brand
from app.api.refusals import coded_refusal
from app.api.set_password_links import (
    link_expires_at,
    mint_employee_link,
    require_link_base,
    send_set_password_email,
    workspace_admin_email,
)
from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import BrandConfig
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.billing.entitlements import live_plan_codes
from app.services.partner_link_token import verify_link_code
from app.services.tenant_provisioning import provision_tenant
from app.tenant import get_tenant, lock_organization
from app.utils.emails import looks_like_email
from app.utils.slug import SlugError, ensure_slug_available, validate_slug_format

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/partner", tags=["partner"])

# Redis key prefix for the single-use consume of a link code's jti. A redeemed
# code can't be replayed (e.g. to re-adopt a child that was detached after the
# first attach) — the jti is burned for the code's whole validity window.
_LINK_CODE_CONSUMED_PREFIX = "partner:link_code:consumed:"


async def _claim_link_code_jti(jti: str) -> bool:
    """Atomically claim a link-code jti. True = first use (proceed); False =
    already consumed. TTL matches the code validity so the key self-expires.

    A Redis outage FAILS CLOSED (returns False → the redeem is rejected): a
    consent handshake must not silently lose its single-use guarantee, so the
    partner re-requests a fresh code rather than risk a replay. (Contrast the
    /api/v1 rate limiter, which fails open — there availability beats the cap;
    here integrity of the org-hierarchy link beats availability.)
    """
    try:
        from app.redis import get_redis

        r = await get_redis()
        ttl = max(1, settings.partner_link_ttl_minutes * 60)
        claimed = await r.set(f"{_LINK_CODE_CONSUMED_PREFIX}{jti}", "1", nx=True, ex=ttl)
        return bool(claimed)
    except Exception as exc:  # pragma: no cover - defensive, fail-closed
        # PII-free: log only the exception class, never its message.
        logger.warning(
            "partner link-code jti claim failed (fail-closed): err=%s", type(exc).__name__
        )
        return False


#: The coded 403 for an admin of a workspace the operator has not made a
#: partner (decisions §574). Distinct from the plain role 403 so the client can
#: say which of the two it is.
PARTNER_CAPABILITY_REQUIRED = "partner_capability_required"


async def require_partner_capability(
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(get_tenant),
) -> Organization:
    """The caller's org, provided the operator has granted it the partner
    capability (``Organization.is_partner``).

    Composed AFTER the admin role gate (``user`` is resolved first), so a
    non-admin still gets the role's plain 403 exactly as before; an admin of a
    workspace that is not a partner gets the coded one. ``org`` is the
    ``get_tenant`` chokepoint's org (JWT ``org`` claim cross-checked), so the
    flag read is the caller's own workspace's, never a header's choice.
    """
    _require_partner(org)
    return org


def _require_partner(org: Organization) -> None:
    """The coded 403 unless ``org`` holds the capability. Attach and provision
    call it again on the row re-read under its lock: the dependency's read is
    unlocked; an operator revoke (`services/partner_capability`)
    counts children under the same row lock, so a link written after this check
    is one the revoke either waited for and counted, or never saw because it
    committed first and this re-read sees ``is_partner`` false (decisions §578)."""
    if not org.is_partner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=coded_refusal(
                PARTNER_CAPABILITY_REQUIRED,
                "This workspace is not set up as a partner.",
            ),
        )


async def _lock_partner_and_child(
    db: AsyncSession, partner: Organization, child_id: uuid.UUID
) -> tuple[Organization, Organization | None]:
    """Lock the partner's row and the child's in ONE statement, in id order.

    The partner's lock is what serializes a link with an operator revoke; the
    child's with another partner adopting it. One fixed order (by id, never
    "the caller's first") means two attaches naming each other's org cannot
    deadlock (decisions §578)."""
    rows = (
        (
            await db.execute(
                select(Organization)
                .where(Organization.id.in_([partner.id, child_id]))
                .order_by(Organization.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.id: row for row in rows}
    return by_id[partner.id], by_id.get(child_id)


class ChildTenantSummary(BaseModel):
    """A single child tenant, as seen by its administering partner."""

    id: uuid.UUID
    name: str
    slug: str
    plan: str
    product_name: str  # the child's resolved white-label product name ("" = default)


class PartnerOverview(BaseModel):
    """The partner's own identity + its child tenants. ``is_partner`` is the
    operator-granted capability (always True here: the route requires it);
    kept on the payload so an older client reading it still works."""

    organization_id: uuid.UUID
    name: str
    is_partner: bool
    children: list[ChildTenantSummary]


async def _resolve_child(
    db: AsyncSession, *, partner_id: uuid.UUID, child_id: uuid.UUID
) -> Organization:
    """Load a child org, enforcing that it is parented by ``partner_id``.

    Data-layer scoping: the WHERE clause requires BOTH the id match AND
    ``parent_org_id == partner_id``, so a partner can only ever reach its own
    children. A non-child (or unknown) id is the SAME opaque 404 so the response
    can't be used to enumerate other tenants' org ids.
    """
    child = (
        await db.execute(
            select(Organization).where(
                Organization.id == child_id,
                Organization.parent_org_id == partner_id,
            )
        )
    ).scalar_one_or_none()
    if child is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Child tenant not found")
    return child


def _child_summary(child: Organization, *, plan: str) -> ChildTenantSummary:
    """``plan`` is the child's LIVE subscription plan code
    (`entitlements.live_plan_codes`) — never the legacy ``Organization.plan``
    string, which no plan change updates (decisions §482)."""
    return ChildTenantSummary(
        id=child.id,
        name=child.name,
        slug=child.slug,
        plan=plan,
        product_name=_resolve_brand(child).product_name,
    )


async def _one_child_summary(db: AsyncSession, child: Organization) -> ChildTenantSummary:
    plans = await live_plan_codes(db, [child.id])
    return _child_summary(child, plan=plans[child.id])


@router.get("", response_model=PartnerOverview)
async def get_partner_overview(
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(require_partner_capability),
    db: AsyncSession = Depends(get_control_db),
) -> PartnerOverview:
    """List the child tenants this partner org administers.

    Admin of a partner workspace only. Scoped to ``parent_org_id == org.id`` —
    never returns a tenant the caller doesn't parent. A partner with no
    children yet gets an empty list.
    """
    children = (
        (
            await db.execute(
                select(Organization)
                .where(Organization.parent_org_id == org.id)
                .order_by(Organization.name)
            )
        )
        .scalars()
        .all()
    )
    # One query for every child's live plan, not one per row.
    plans = await live_plan_codes(db, [c.id for c in children])
    return PartnerOverview(
        organization_id=org.id,
        name=org.name,
        is_partner=org.is_partner,
        children=[_child_summary(c, plan=plans[c.id]) for c in children],
    )


@router.get("/children/{child_id}/branding", response_model=BrandConfig)
async def get_child_branding(
    child_id: uuid.UUID,
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(require_partner_capability),
    db: AsyncSession = Depends(get_control_db),
) -> BrandConfig:
    """Read one child tenant's white-label branding.

    Admin-only; the child must be parented by the caller (else opaque 404). The
    resolver tolerates a missing / malformed brand block by returning all-empty
    (= platform defaults), identical to the child's own GET.
    """
    child = await _resolve_child(db, partner_id=org.id, child_id=child_id)
    return _resolve_brand(child)


@router.put("/children/{child_id}/branding", response_model=BrandConfig)
async def update_child_branding(
    child_id: uuid.UUID,
    body: BrandConfig,
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(require_partner_capability),
    db: AsyncSession = Depends(get_control_db),
) -> BrandConfig:
    """Push branding onto one child tenant. Admin-only; audited into the CHILD's trail.

    The child must be parented by the caller (else opaque 404). Pydantic has
    already validated the payload (hex colors, http(s) URLs). The write lands on
    the CHILD org's ``settings.brand`` (control plane) and PRESERVES
    ``custom_domains`` — the same carry-forward the child's own
    ``PUT /api/organization/branding`` does, so a partner-initiated brand save
    can't silently wipe the child's registered vanity hostnames. The audit row
    is written into the CHILD's tenant trail (``organization.branding_updated``,
    with ``via: "partner"`` + the acting partner org id so the change is
    attributable) — PII-free, booleans only.
    """
    child = await _resolve_child(db, partner_id=org.id, child_id=child_id)
    # Serialise with every other writer of the CHILD's settings (`lock_organization`).
    child = await lock_organization(db, child)

    existing = dict(child.settings or {})
    prior_brand = existing.get("brand")
    new_brand = body.model_dump()
    if isinstance(prior_brand, dict) and "custom_domains" in prior_brand:
        new_brand["custom_domains"] = prior_brand["custom_domains"]
    existing["brand"] = new_brand
    child.settings = existing
    # Mutating a nested dict in-place doesn't mark JSONB dirty on its own.
    flag_modified(child, "settings")

    await db.commit()

    # Audit into the CHILD's tenant trail (where every other mutation for that
    # tenant lands), not the partner's — the branding belongs to the child.
    # PII-free: which fields are now set (booleans), plus the partner attribution.
    await dispatch_auth_audit(
        organization_id=child.id,
        actor_id=user.id,
        action="organization.branding_updated",
        entity_type="organization",
        entity_id=child.id,
        details={
            "via": "partner",
            "partner_org_id": str(org.id),
            "product_name_set": bool(body.product_name),
            "logo_url_set": bool(body.logo_url),
            "accent_color_set": bool(body.accent_color),
            "support_url_set": bool(body.support_url),
            "legal_url_set": bool(body.legal_url),
        },
    )

    return body


# ---------------------------------------------------------------------------
# Provisioning the parent/child link — two-sided consent (attach + detach).
#
# Authorization model (the load-bearing privilege boundary): a partner must NOT
# be able to unilaterally declare an arbitrary org its child — that would be a
# cross-tenant takeover. So attach requires the prospective child's OWN admin to
# first mint a short-lived, HMAC-signed *link code* (proof of consent — minted
# in Organization settings, `POST /api/organization/partner-link-code`, by any
# admin, partner or not, decisions §574); the
# partner's admin then redeems that code to attach. The signature (key held by
# the platform via sops/KMS) is what makes this safe — a partner can't forge a
# code or aim it at an org that never consented. A child that already has a
# parent can't be re-parented without an explicit detach (no silent takeover).
# ---------------------------------------------------------------------------


class AttachChildRequest(BaseModel):
    """Redeem a child-issued link code to attach that child to the caller."""

    link_code: str = Field(min_length=1, max_length=4096)


class ProvisionChildRequest(BaseModel):
    """Provision a brand-NEW child tenant under the calling partner org.

    Mirrors ``scripts/create_tenant.py`` inputs: a company name, a URL slug, and
    the first admin's email. There is no admin password at all until the admin
    chooses one through the emailed set-password link — a partner never sets or
    sees another org's password, and no secret travels in this request.
    """

    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=63)
    admin_email: str = Field(min_length=3, max_length=320)
    admin_name: str | None = Field(default=None, max_length=200)
    # Legacy, kept so existing callers do not 422: stored on the legacy
    # `Organization.plan` label only. Every new tenant starts on the real Free
    # subscription, and the summary reports that live plan (decisions §482).
    plan: str = Field(default="free", max_length=50)


class ProvisionChildResponse(ChildTenantSummary):
    """The newly provisioned child, and whether its admin was sent their link.

    No credential: the child's admin is provisioned with no password and emailed
    a single-use set-password link (`docs/decisions.md` §283) — the partner
    never holds a working credential for the child. ``set_password_email_sent``
    is false when the link could not be minted or mailed AFTER the tenant
    already existed; the admin then uses Forgot password on the child's sign-in
    page, which works for the account like any other.
    """

    admin_email: str
    set_password_email_sent: bool
    set_password_link_expires_at: datetime | None = None


@router.post(
    "/children/provision",
    response_model=ProvisionChildResponse,
    status_code=status.HTTP_201_CREATED,
)
async def provision_child(
    body: ProvisionChildRequest,
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(require_partner_capability),
    db: AsyncSession = Depends(get_control_db),
) -> ProvisionChildResponse:
    """Provision a brand-new child tenant already parented to the caller.

    Admin-only; the caller is its OWN partner org via the ``get_tenant``
    chokepoint (JWT ``org``-claim cross-check), so a partner can only ever
    provision a child UNDER ITSELF — there is no way to point the new tenant at a
    different parent (no ``parent_org_id`` is accepted from the client; it is
    always ``org.id``). This is the new-tenant counterpart of ``attach_child``:
    attach adopts an *existing* consenting org, this *creates* a fresh one.

    Flow: validate the slug (format + reserved-word + availability) → provision
    the full tenant via the shared ``provision_tenant`` primitive (control-plane
    org + admin user + the ``feoh_<slug>`` tenant DB + tables, with its own
    drop-the-orphan-DB rollback on any partial failure) → stamp
    ``parent_org_id = org.id`` on the new org → audit ``partner.child_provisioned``
    on BOTH trails (the partner's and the new child's), PII-free (org ids + slug
    only, never the admin email or password).

    Errors mirror the siblings + signup: an invalid slug is a 422, a taken slug a
    409 (the only enumeration surface here is the partner's OWN choice of slug,
    which signup already exposes publicly — not a cross-tenant leak).
    """
    # Shape only, from the one owner in `app/utils/emails.py` — the address is
    # the new admin's login; the real check is that admin clicking their
    # set-password link.
    if not looks_like_email(body.admin_email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter a valid admin email address.",
        )

    # Validate slug shape (format + reserved words) before doing any work.
    try:
        validate_slug_format(body.slug)
    except SlugError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # Availability check (re-checked transactionally below by the unique slug
    # column; this gives a clean 409 before we spin up a DB).
    try:
        await ensure_slug_available(body.slug, db)
    except SlugError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Nowhere to point the admin's set-password link (or forgot-password, its
    # fallback) → refuse before a database is created, as signup does.
    require_link_base(body.slug, None)

    admin_name = body.admin_name or f"{body.name} Admin"

    # Lock the partner's row for the rest of this transaction and re-check the
    # capability under it, so an operator revoke (which counts children under the
    # same lock) cannot commit between this check and the parent link below
    # (decisions §578). Held across provisioning: the new tenant is born
    # parented, or not at all.
    org = await lock_organization(db, org)
    _require_partner(org)

    # provision_tenant owns the orphan-DB rollback: if the control-plane insert
    # or tenant-table creation fails after CREATE DATABASE, it drops the DB it
    # created. We never half-create. A slug that raced past the pre-check trips
    # the unique constraint inside provisioning → IntegrityError → clean 409.
    try:
        result = await provision_tenant(
            company_name=body.name,
            slug=body.slug,
            admin_email=body.admin_email,
            admin_name=admin_name,
            # No password: the child's admin chooses one through the emailed
            # link, so the partner never holds a credential for the child.
            admin_password=None,
            plan=body.plan,
            must_change_password=False,
        )
    except IntegrityError as exc:
        # Two provisions racing the same slug (or email) — the loser gets a 409,
        # not a 500. provision_tenant already dropped its orphan DB.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{body.slug}' is already taken.",
        ) from exc

    # Stamp the parent link on the freshly created org. This is the whole point
    # of the endpoint over plain provisioning — the new tenant is born parented.
    child = await db.get(Organization, result.organization_id)
    if child is None:  # pragma: no cover - provisioning just created it
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Provisioning succeeded but the new organization could not be loaded.",
        )
    child.parent_org_id = org.id
    await db.commit()

    # The child admin's set-password link. Best-effort, unlike the admin and
    # portal invites: the tenant and its database already exist and cannot be
    # unwound here, so a failure is reported on the response instead and the
    # admin's way in is Forgot password, which needs only the active account
    # and the base URL checked above.
    email_sent = await _send_child_admin_link(
        child, user_id=result.user_id, admin_name=admin_name, admin_email=body.admin_email
    )

    # Audit BOTH org trails, PII-free (org ids + slug only — never the admin
    # email or the set-password link).
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="partner.child_provisioned",
        entity_type="organization",
        entity_id=child.id,
        details={
            "child_org_id": str(child.id),
            "child_slug": child.slug,
            "set_password_email_sent": email_sent,
        },
    )
    await dispatch_auth_audit(
        organization_id=child.id,
        actor_id=user.id,
        action="partner.parent_linked",
        entity_type="organization",
        entity_id=child.id,
        details={"partner_org_id": str(org.id), "via": "provision"},
    )

    summary = await _one_child_summary(db, child)
    return ProvisionChildResponse(
        **summary.model_dump(),
        admin_email=body.admin_email,
        set_password_email_sent=email_sent,
        set_password_link_expires_at=link_expires_at() if email_sent else None,
    )


async def _send_child_admin_link(
    child: Organization, *, user_id: uuid.UUID, admin_name: str, admin_email: str
) -> bool:
    """Mint and mail the child admin's set-password link; ``False`` on any
    failure (logged by slug and ids only — never the address or the link)."""
    try:
        link = await mint_employee_link(child.slug, child.settings, user_id, set_password=True)
        await send_set_password_email(
            workspace_admin_email(
                org_name=child.name, admin_name=admin_name, admin_email=admin_email, link=link
            ),
            log_ref=f"child_org={child.id}",
        )
    except HTTPException:
        logger.warning(
            "Child tenant provisioned without a delivered set-password link (slug=%s)",
            child.slug,
        )
        return False
    return True


@router.post("/children", response_model=ChildTenantSummary, status_code=status.HTTP_201_CREATED)
async def attach_child(
    body: AttachChildRequest,
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(require_partner_capability),
    db: AsyncSession = Depends(get_control_db),
) -> ChildTenantSummary:
    """Attach a consenting child tenant to the caller's partner org.

    Admin-only. The privilege boundary: the only way to reach a child here is to
    present a valid, unexpired, unconsumed link code that the CHILD's own admin
    minted (``POST /api/organization/partner-link-code``) — so a partner can never adopt an
    org that didn't consent. The signature is verified first; an invalid /
    expired / wrong-purpose code is one opaque 400 (no enumeration). The jti is
    then claimed single-use in Redis (fail-closed) so a code can't be replayed.

    Guards, in order:
      * The signed code → the consenting child's org id (else opaque 400).
      * Single-use claim on the jti (else 409 — already redeemed).
      * The child org must exist (defensive 400 — the signed id is platform-
        minted, so this only trips on a deleted org).
      * **Re-parent guard**: a child already linked to a parent is 409 — no
        silent takeover. Re-linking to the SAME caller is the idempotent no-op
        (returns 201 with the summary), so a double-submit is safe.
      * A partner can't adopt ITSELF (400) — a self-FK loop is nonsensical.

    The link is the control-plane write ``child.parent_org_id = org.id``. It is
    audited PII-free on BOTH trails: ``partner.child_attached`` on the partner's
    (with the child org id) and ``partner.parent_linked`` on the child's (with
    the partner org id) — so a SOX query against either tenant sees the change.
    """
    decoded = verify_link_code(body.link_code, settings.partner_link_signing_key)
    if decoded is None:
        # One opaque 400 for every bad-code path (disabled / forged / expired /
        # wrong purpose) — never reveals which.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired link code.",
        )

    child_id = decoded.child_org_id

    if child_id == org.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An organization cannot be its own partner.",
        )

    # Burn the single-use code BEFORE the mutation. If a later guard rejects the
    # attach the code stays consumed — a rejected handshake doesn't get a free
    # retry of the same code; the child re-issues. Fail-closed on a Redis blip.
    if not await _claim_link_code_jti(decoded.jti):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This link code has already been used.",
        )

    # Lock both rows (one statement, id order) and re-check the capability under
    # the partner's lock: an operator revoke counts children under that same
    # lock, so it either waits for this link and refuses, or commits first and
    # this re-read refuses (decisions §578).
    org, child = await _lock_partner_and_child(db, org, child_id)
    _require_partner(org)
    if child is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired link code.",
        )

    # Re-parent guard. Idempotent if already OUR child; refuse if someone else's.
    if child.parent_org_id is not None:
        if child.parent_org_id == org.id:
            return await _one_child_summary(db, child)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That tenant is already linked to a partner.",
        )

    child.parent_org_id = org.id
    await db.commit()

    # Audit BOTH org trails, PII-free (org ids only — never names / slugs).
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="partner.child_attached",
        entity_type="organization",
        entity_id=child.id,
        details={"child_org_id": str(child.id)},
    )
    await dispatch_auth_audit(
        organization_id=child.id,
        actor_id=user.id,
        action="partner.parent_linked",
        entity_type="organization",
        entity_id=child.id,
        details={"partner_org_id": str(org.id)},
    )
    return await _one_child_summary(db, child)


@router.delete("/children/{child_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_child(
    child_id: uuid.UUID,
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org: Organization = Depends(require_partner_capability),
    db: AsyncSession = Depends(get_control_db),
) -> None:
    """Detach a child tenant from the caller's partner org.

    Admin-only; scoped at the data layer to the caller's OWN children via
    ``_resolve_child`` (``parent_org_id == org.id``), so a partner can only ever
    detach a tenant it actually parents — a non-child / unknown id is the same
    opaque 404 as everywhere on this surface (no enumeration). Sets
    ``parent_org_id = NULL`` (back to standalone). Audited PII-free on BOTH
    trails. Idempotent: ``_resolve_child`` only matches while the link exists, so
    a second detach is a clean 404 (the link is already gone) — never a 500.
    """
    child = await _resolve_child(db, partner_id=org.id, child_id=child_id)

    child.parent_org_id = None
    await db.commit()

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="partner.child_detached",
        entity_type="organization",
        entity_id=child.id,
        details={"child_org_id": str(child.id)},
    )
    await dispatch_auth_audit(
        organization_id=child.id,
        actor_id=user.id,
        action="partner.parent_unlinked",
        entity_type="organization",
        entity_id=child.id,
        details={"partner_org_id": str(org.id)},
    )
