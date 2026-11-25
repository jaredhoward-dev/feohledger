"""Operator: grant or revoke an organization's partner (reseller) capability.

``Organization.is_partner`` (migration 0151, ``docs/decisions.md`` §574) is what
every ``/api/partner`` route requires and what shows the Partner Admin row.
Acting as a reseller — provisioning, adopting and branding other workspaces —
is a commercial arrangement the operator agrees with a customer, so nothing in
the API writes the flag; this module is its one writer, driven by
``scripts/set_partner_capability.py``.

Each change writes ``organization.partner_capability_granted`` / ``_revoked``
to the org's own audit trail **before** it changes anything, and refuses to
proceed if the row cannot be written (the break-glass rule: an operator edit to
what a tenant may do, with no record, is the outcome to avoid).

A revoke is refused while the org still parents a child: the children would be
left administered by nobody (their partner could no longer reach
``/api/partner`` to detach them). The partner's admin detaches them first.
The count is taken under the org's row lock, which ``attach_child`` and
``provision_child`` also take (and re-check ``is_partner`` under) before they
link a child, so a revoke and a new link serialize: one of them refuses
(decisions §578).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.services.audit_dispatch import record_auth_audit_or_raise

ACTION_GRANTED = "organization.partner_capability_granted"
ACTION_REVOKED = "organization.partner_capability_revoked"
MAX_REASON = 500


class TenantNotFound(LookupError):
    """No organization has the given slug."""


class AuditWriteFailed(RuntimeError):
    """The audit row could not be written, so nothing was changed."""


class PartnerHasChildren(RuntimeError):
    """A revoke was refused: the org still parents ``count`` child tenants."""

    def __init__(self, count: int) -> None:
        super().__init__(f"{count} child tenant(s) still linked")
        self.count = count


@dataclass(frozen=True)
class CapabilityResult:
    slug: str
    organization_id: uuid.UUID
    is_partner: bool
    # False when the org already held the requested value: no write, no audit row.
    changed: bool


async def set_partner_capability(
    session: AsyncSession, slug: str, *, enabled: bool, reason: str | None = None
) -> CapabilityResult:
    """Set ``is_partner`` for the tenant ``slug``, audited first. Idempotent.

    ``reason`` (a contract / ticket reference) is recorded in the audit row;
    keep personal data out of it.
    """
    org = (
        await session.execute(
            select(Organization).where(Organization.slug == slug).with_for_update()
        )
    ).scalar_one_or_none()
    if org is None:
        raise TenantNotFound(slug)
    org_id = org.id

    if org.is_partner == enabled:
        await session.rollback()
        return CapabilityResult(slug, org_id, is_partner=enabled, changed=False)

    if not enabled:
        children = (
            await session.execute(
                select(func.count())
                .select_from(Organization)
                .where(Organization.parent_org_id == org_id)
            )
        ).scalar_one()
        if children:
            await session.rollback()
            raise PartnerHasChildren(children)

    details: dict = {"source": "operator"}
    if reason and reason.strip():
        details["reason"] = reason.strip()[:MAX_REASON]
    # The row first: if the trail cannot record the change, it does not happen.
    # The row lock above is held across this write.
    try:
        await record_auth_audit_or_raise(
            organization_id=org_id,
            actor_id=None,
            action=ACTION_GRANTED if enabled else ACTION_REVOKED,
            entity_type="organization",
            entity_id=org_id,
            details=details,
        )
    except Exception as exc:
        await session.rollback()
        raise AuditWriteFailed(exc.__class__.__name__) from exc

    org.is_partner = enabled
    await session.commit()
    return CapabilityResult(slug, org_id, is_partner=enabled, changed=True)
