"""Out-of-office delegation: who may stand in for an approver (decisions §511).

The one eligibility predicate for a delegate. It is read at every place a
delegation is either accepted or followed:

* ``POST /api/auth/delegation`` (the write) and ``GET
  /api/auth/delegation/candidates`` (its picker), so the picker never offers
  someone the write would refuse;
* ``approval_chain.resolve_assignee`` (routing: ``review.assign_reviewer``) and
  ``approval_chain._resolve_authorized_approvers`` (a named approver's delegate
  clearing their level), so a delegation that WAS valid when saved is
  re-checked every time it is followed.

The re-check is the point. A delegation is stored once and followed for days;
in between the delegate can be deactivated, lose ``invoice.approve`` (a role
edit or a SCIM push), or — for a row saved before §511 added the check — never
have held it. Following such a delegation strands the invoice with someone who
can never act on it, so routing instead keeps the original reviewer.

Whether the delegate can clear one PARTICULAR invoice (segregation of duties,
the money gates) is a further, per-invoice question answered by
``review.delegate_refusal`` from the approval path's own gates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import User


def can_approve(candidate) -> bool:
    """Whether ``candidate``'s roles grant ``invoice.approve`` — resolved through
    ``effective_permissions``, so a custom role granted it qualifies and an
    ap_clerk does not. ``candidate.roles`` must be loaded."""
    from app.api.permissions import PERM_INVOICE_APPROVE, effective_permissions

    return PERM_INVOICE_APPROVE in effective_permissions(getattr(candidate, "roles", None))


def is_eligible_delegate(delegator, delegate) -> bool:
    """Whether ``delegate`` may stand in for ``delegator``: exists, is not the
    delegator, is in the delegator's organization, is active, and can approve."""
    return (
        delegate is not None
        and delegate.id != delegator.id
        and delegate.organization_id == delegator.organization_id
        and bool(delegate.is_active)
        and can_approve(delegate)
    )


def delegation_in_force(user, *, now: datetime | None = None) -> bool:
    """Whether ``user`` has a delegation whose window is still open."""
    now = now or datetime.now(UTC)
    return bool(user.delegate_to_id and user.delegate_until and user.delegate_until > now)


async def eligible_delegate_of(user_id: uuid.UUID, control_db: AsyncSession) -> User | None:
    """The delegate an assignment to ``user_id`` should go to right now, or
    ``None`` — no such user, no delegation in force, or a delegate who fails
    :func:`is_eligible_delegate` today. Returned with ``roles`` loaded."""
    delegator = (
        await control_db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if delegator is None or not delegation_in_force(delegator):
        return None
    delegate = (
        await control_db.execute(
            select(User)
            .options(selectinload(User.roles))
            .where(User.id == delegator.delegate_to_id)
        )
    ).scalar_one_or_none()
    if not is_eligible_delegate(delegator, delegate):
        return None
    return delegate
