"""A move to a free plan waits for the end of the paid period (decisions §483).

`change_plan` used to cancel the provider subscription the moment an admin
chose Free, crediting the unused part of the period to a customer balance that
a free workspace has no future invoice to spend it on. Now a move from a paid
plan to a free one is **scheduled**:

* :func:`schedule_move_to_free` (called by `change_plan`) tells a live provider
  to end the subscription at its period end (Stripe ``cancel_at_period_end``:
  no proration, no credit, the period's metered AI-read overage still billed on
  the final invoice) and records the move on our row —
  ``Subscription.pending_plan_id`` / ``pending_plan_effective_at`` — with a
  ``billing.plan_change_scheduled`` audit row. The row stays ``active`` on its
  paid plan, entitlements intact, until then.
* :func:`keep_current_plan` is the undo ("Keep Growth"): the provider's
  scheduled cancel is cleared and so is our pending state, audited
  ``billing.scheduled_change_canceled``. A paid → paid change made while a move
  is pending clears it too (every paid amend sends ``cancel_at_period_end=false``).
* :func:`apply_scheduled_downgrade` makes the move when the period has ended:
  the row is repointed onto the free plan, live, with no provider id — the
  shape an immediate cancel always left. Two callers, one rule: the provider's
  ``customer.subscription.deleted`` webhook (`webhook_processing`) and, as the
  backstop for a lost webhook or for the ``mock`` provider, the dunning sweep
  (`dunning_sweep`). Each writes its own ``billing.plan_changed`` audit row.

Which moves wait (:func:`waits_for_period_end`): paid → free on an ``active``
or ``trialing`` subscription. A ``past_due`` one has not paid for the period it
would be kept in, so it still moves at once, as does a live-provider row with
no provider subscription (nothing to end later; `provider_sync` finds and
cancels any stray). Paid → paid is unchanged: immediate and prorated, because
that credit or charge lands on the next invoice the org will actually pay.

With ``mock`` (local, CI) nothing is told to a provider; the row is the whole
state and the dunning sweep applies the move once the period is over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Plan, Subscription
from app.models.organization import Organization
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.billing.entitlements import get_active_subscription
from app.services.billing.period import add_months, current_period
from app.services.billing.plan_catalog import clear_stale_canceled_subscription
from app.services.billing.provider_sync import (
    ProviderLegRefused,
    calendar_month,
    claim_plan_change_lease,
    is_live_provider,
    lease_is_ours,
    plan_is_paid,
    release_plan_change_lease,
)
from app.services.billing.provisioning import billing_adapter_for
from app.services.billing_adapters.base import BillingAdapter, ProviderSubscription

logger = logging.getLogger(__name__)

#: Statuses whose paid period is kept until it ends. ``past_due`` is not one:
#: the period it would keep has not been paid for.
SCHEDULABLE_STATUSES = frozenset({"active", "trialing"})

#: The Keep refusal once the move's moment has passed — the provider has ended
#: (or is ending) the subscription, so there is nothing left to resume.
ALREADY_DUE = "scheduled_change_already_due"


def waits_for_period_end(
    subscription: Subscription, current_plan: Plan, new_plan: Plan, *, live: bool
) -> bool:
    """Whether moving ``subscription`` from ``current_plan`` to ``new_plan``
    is scheduled for the period end rather than made now. One predicate for
    the preview and the change, so the two always describe the same move."""
    if not plan_is_paid(current_plan) or plan_is_paid(new_plan):
        return False
    if subscription.status not in SCHEDULABLE_STATUSES:
        return False
    # With a live provider the end of the period is the provider's to enforce;
    # a row that names no provider subscription has nothing to end later.
    return not (live and subscription.external_subscription_id is None)


def estimated_effective_at(subscription: Subscription, *, live: bool, now: datetime) -> datetime:
    """When a move scheduled at ``now`` would take effect: the end of the
    current paid period. With a live provider that is the window the provider
    last reported (copied onto the row by every change and webhook), else the
    calendar month every live subscription is anchored to; with ``mock`` it is
    the row's own period (`period.current_period`). The provider's answer to
    the schedule call is what is finally recorded."""
    if live:
        end = subscription.current_period_end
        if end is not None and end > now:
            return end
        return calendar_month(now)[1]
    return current_period(subscription, now=now).end


def _plain(subscription: Subscription) -> dict:
    """The values a scheduling call needs after a commit expires the row."""
    updated_at = subscription.updated_at or subscription.created_at
    return {
        "id": subscription.id,
        "plan_id": subscription.plan_id,
        "external_id": subscription.external_subscription_id,
        "pending_plan_id": subscription.pending_plan_id,
        "status": subscription.status,
        "stamp": int(updated_at.timestamp()) if updated_at else 0,
    }


#: The fields a provider call is made on the strength of. Re-read under the
#: lease, before the call: the lease serialises every live plan change, so a
#: row that still matches its peek then cannot move before the call is made.
_PEEKED = ("plan_id", "external_id", "pending_plan_id", "status")


async def _fresh(control_db: AsyncSession, subscription_id) -> dict | None:
    """The row's peeked fields as committed now (not the identity map's)."""
    row = (
        await control_db.execute(
            select(
                Subscription.plan_id,
                Subscription.external_subscription_id,
                Subscription.pending_plan_id,
                Subscription.status,
                Subscription.pending_plan_effective_at,
            ).where(Subscription.id == subscription_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return {
        "plan_id": row.plan_id,
        "external_id": row.external_subscription_id,
        "pending_plan_id": row.pending_plan_id,
        "status": row.status,
        "effective_at": row.pending_plan_effective_at,
    }


def _matches(peek: dict, fresh: dict | None) -> bool:
    return fresh is not None and all(peek[k] == fresh[k] for k in _PEEKED)


async def schedule_move_to_free(
    control_db: AsyncSession,
    *,
    org: Organization,
    adapter: BillingAdapter,
    subscription: Subscription,
    current_plan: Plan,
    new_plan: Plan,
    actor_id,
    now: datetime,
    quote_proration_date: datetime | None,
):
    """Schedule ``subscription``'s move onto the free ``new_plan`` for the end
    of its period. Returns `plan_change.PlanChangeResult` with
    ``scheduled=True``.

    With a live provider: under the org's plan-change lease (the provider call
    is outside the row lock, as every provider leg is — `docs/billing.md`
    § Concurrency), ``cancel_at_period_end`` is set at the provider, then the
    row is locked, re-checked and stamped with the pending plan and the
    provider's period end. The ``Idempotency-Key`` is per change of the row
    (row id + last update + target), so a retry replays and the next schedule
    after a Keep does not.
    """
    # Imported here: `plan_change` imports this module.
    from app.services.billing.plan_change import (
        PlanChangeError,
        PlanChangeResult,
        _get_active_subscription_for_update,
        _zero_proration,
    )

    org_id = org.id
    live = is_live_provider(adapter)
    peek = _plain(subscription)
    old_price, new_price = current_plan.monthly_price, new_plan.monthly_price
    new_plan_id, new_plan_code = new_plan.id, new_plan.code
    estimate = estimated_effective_at(subscription, live=live, now=now)

    def _already_scheduled(effective: datetime | None):
        return PlanChangeResult(
            changed=False,
            old_plan_code=old_plan_code_peek,
            new_plan_code=new_plan_code,
            proration=_zero_proration(old_price, new_price),
            reason="already_scheduled",
            scheduled=True,
            effective_at=effective,
        )

    old_plan_code_peek = current_plan.code
    lease: str | None = None
    provider_sub: ProviderSubscription | None = None
    try:
        if live:
            lease = await claim_plan_change_lease(control_db, org, now=now)
            # Under the lease, before Stripe is told anything: the row must
            # still be what the peek saw (another change may have moved it,
            # or a double submit may have scheduled this move already).
            fresh = await _fresh(control_db, peek["id"])
            if fresh is not None and fresh["pending_plan_id"] == new_plan_id:
                await control_db.rollback()
                return _already_scheduled(fresh["effective_at"])
            if not _matches(peek, fresh):
                raise ProviderLegRefused("plan_change_in_progress")
            key = f"feohledger-sub-schedule-free-{peek['id']}-{peek['stamp']}-{new_plan_code}"
            provider_sub = await adapter.cancel_subscription_at_period_end(
                peek["external_id"], idempotency_key=key
            )
        if lease is not None and not await lease_is_ours(control_db, org_id, lease):
            logger.error("billing downgrade schedule outlived its lease; not recorded")
            raise ProviderLegRefused("plan_change_in_progress")
        active = await _get_active_subscription_for_update(control_db, org_id)
        if active is None:
            raise PlanChangeError("no_live_subscription")
        row, plan = active
        if row.id == peek["id"] and row.pending_plan_id == new_plan_id and provider_sub is None:
            # A double submit on `mock` (no lease): the first one recorded it.
            effective = row.pending_plan_effective_at
            await control_db.rollback()
            return _already_scheduled(effective)
        if (
            row.id != peek["id"]
            or plan.id != peek["plan_id"]
            or row.external_subscription_id != peek["external_id"]
            or row.pending_plan_id != peek["pending_plan_id"]
            or row.status not in SCHEDULABLE_STATUSES
        ):
            # Another writer moved the row since the peek: what the provider
            # was told no longer describes it.
            raise ProviderLegRefused("plan_change_in_progress")

        effective_at = estimate
        if provider_sub is not None and provider_sub.current_period_end is not None:
            # The provider ends it at ITS period end; that is the date.
            effective_at = provider_sub.current_period_end
            if provider_sub.current_period_start is not None:
                row.current_period_start = provider_sub.current_period_start
                row.current_period_end = provider_sub.current_period_end
        elif not live:
            # Persist the window the date was read from (`current_period`).
            period = current_period(row, now=now)
            row.current_period_start, row.current_period_end = period.start, period.end

        previous_pending = row.pending_plan_id
        row.pending_plan_id = new_plan_id
        row.pending_plan_effective_at = effective_at
        details = {
            "from_plan": plan.code,
            "to_plan": new_plan_code,
            "effective_at": effective_at.isoformat(),
            "provider": adapter.provider_name,
            # Nothing is prorated, credited or refunded: the paid period is kept.
            "proration_amount": "0.00",
        }
        if quote_proration_date is not None:
            details["quote_proration_date"] = quote_proration_date.isoformat()
        if provider_sub is not None:
            details["provider_action"] = "cancel_at_period_end"
            details["external_subscription_id"] = row.external_subscription_id
        if previous_pending is not None and previous_pending != new_plan_id:
            details["replaced_pending_plan_id"] = str(previous_pending)
        # Append-only, before the commit (as `change_plan` does).
        await dispatch_auth_audit(
            organization_id=org_id,
            actor_id=actor_id,
            action="billing.plan_change_scheduled",
            entity_id=row.id,
            details=details,
        )
        old_plan_code = plan.code
        await control_db.commit()
    except BaseException:
        await control_db.rollback()
        if provider_sub is not None:
            # Stripe will end the subscription at the period end and our row
            # does not say so. Its `deleted` webhook then cancels the row (the
            # org reads as Free) — the right end state, but unannounced; a
            # retry of the same change replays the provider call and records.
            logger.error("billing downgrade was scheduled at the provider but not recorded")
        raise
    finally:
        if lease is not None:
            await release_plan_change_lease(control_db, org_id, lease)

    return PlanChangeResult(
        changed=True,
        old_plan_code=old_plan_code,
        new_plan_code=new_plan_code,
        proration=_zero_proration(old_price, new_price),
        scheduled=True,
        effective_at=effective_at,
    )


@dataclass(frozen=True)
class KeepResult:
    """Outcome of :func:`keep_current_plan`. ``cleared`` is False when there
    was nothing pending (an idempotent retry)."""

    cleared: bool
    plan_code: str


async def keep_current_plan(
    control_db: AsyncSession, *, org: Organization, actor_id, now: datetime | None = None
) -> KeepResult:
    """Cancel the org's pending move to a free plan ("Keep Growth").

    Refusals: `plan_change.PlanChangeError` (no live subscription — the API's
    404), :class:`ProviderLegRefused` ``scheduled_change_already_due`` (the
    period has ended; the provider is ending or has ended the subscription),
    ``billing_not_configured`` (the row is billed by a provider this org no
    longer resolves to) and ``plan_change_in_progress``. With a live provider
    ``cancel_at_period_end`` is cleared first, under the lease; nothing is
    charged. Idempotent: nothing pending is a successful no-op.
    """
    from app.services.billing.plan_change import (
        PlanChangeError,
        _get_active_subscription_for_update,
    )

    now = now or datetime.now(UTC)
    org_id = org.id
    active = await get_active_subscription(control_db, org_id)
    if active is None:
        raise PlanChangeError("no_live_subscription")
    subscription, plan = active
    if subscription.pending_plan_id is None:
        return KeepResult(cleared=False, plan_code=plan.code)
    effective_at = subscription.pending_plan_effective_at
    if effective_at is not None and effective_at <= now:
        raise ProviderLegRefused(ALREADY_DUE)

    adapter = billing_adapter_for(org)
    live = is_live_provider(adapter)
    peek = _plain(subscription)
    if not live and peek["external_id"] is not None:
        # Stripe ends this subscription at the period end and nothing here can
        # tell it otherwise (the org's provider resolves to `mock`).
        raise ProviderLegRefused("billing_not_configured")

    pending_target = peek["pending_plan_id"]

    async def _gone(row_id, plan_id, current_code: str) -> KeepResult:
        """The pending move is gone by the time we look under the lease or
        lock: applied (the org is on the target now) is a refusal; taken back
        by a paid change (or a racing Keep) is the no-op it amounts to."""
        await control_db.rollback()
        if row_id == peek["id"] and plan_id == pending_target:
            raise ProviderLegRefused(ALREADY_DUE)
        return KeepResult(cleared=False, plan_code=current_code)

    lease: str | None = None
    provider_sub: ProviderSubscription | None = None
    try:
        if live and peek["external_id"] is not None:
            lease = await claim_plan_change_lease(control_db, org, now=now)
            fresh = await _fresh(control_db, peek["id"])
            if fresh is not None and fresh["pending_plan_id"] is None:
                current_now = await control_db.get(Plan, fresh["plan_id"])
                return await _gone(
                    peek["id"], fresh["plan_id"], current_now.code if current_now else ""
                )
            if not _matches(peek, fresh):
                raise ProviderLegRefused("plan_change_in_progress")
            provider_sub = await adapter.resume_subscription(
                peek["external_id"],
                idempotency_key=f"feohledger-sub-resume-{peek['id']}-{peek['stamp']}",
            )
        if lease is not None and not await lease_is_ours(control_db, org_id, lease):
            logger.error("billing keep-plan outlived its lease; not recorded")
            raise ProviderLegRefused("plan_change_in_progress")
        locked = await _get_active_subscription_for_update(control_db, org_id)
        if locked is None:
            raise PlanChangeError("no_live_subscription")
        row, current = locked
        if row.id != peek["id"] or row.pending_plan_id is None:
            # Applied or cleared meanwhile (the webhook, the sweep, a racer).
            # Plain values first: the rollback expires the loaded rows.
            return await _gone(row.id, row.plan_id, current.code)
        if row.external_subscription_id != peek["external_id"]:
            raise ProviderLegRefused("plan_change_in_progress")
        pending_plan = await control_db.get(Plan, row.pending_plan_id)
        details = {
            "plan": current.code,
            "canceled_to_plan": pending_plan.code if pending_plan else None,
            "canceled_effective_at": (
                row.pending_plan_effective_at.isoformat() if row.pending_plan_effective_at else None
            ),
            "provider": adapter.provider_name,
            "reason": "kept_by_customer",
        }
        if provider_sub is not None:
            details["provider_action"] = "resumed"
            details["external_subscription_id"] = row.external_subscription_id
        clear_pending(row)
        await dispatch_auth_audit(
            organization_id=org_id,
            actor_id=actor_id,
            action="billing.scheduled_change_canceled",
            entity_id=row.id,
            details=details,
        )
        plan_code = current.code
        await control_db.commit()
    except BaseException:
        await control_db.rollback()
        if provider_sub is not None:
            # The provider renews the subscription and our row still says it
            # moves to Free: the sweep drops the stale pending state once the
            # date passes and the provider has not ended it.
            logger.error("billing scheduled cancel was cleared at the provider but not recorded")
        raise
    finally:
        if lease is not None:
            await release_plan_change_lease(control_db, org_id, lease)
    return KeepResult(cleared=True, plan_code=plan_code)


def clear_pending(subscription: Subscription) -> None:
    """Forget a scheduled move (both columns, together)."""
    subscription.pending_plan_id = None
    subscription.pending_plan_effective_at = None


async def apply_scheduled_downgrade(
    control_db: AsyncSession, subscription: Subscription, *, now: datetime
) -> dict:
    """Make ``subscription``'s scheduled move now. The caller holds the row
    ``FOR UPDATE``, checked it has a pending plan, writes the audit row (with
    the returned PII-free details) and commits.

    The row ends on the free plan, ``active``, with no provider id and a
    fresh monthly window starting at the moment the move was due — the shape
    the immediate cancel always left, so entitlements, the summary and the next
    plan change read it the same way.
    """
    pending_plan = await control_db.get(Plan, subscription.pending_plan_id)
    if pending_plan is None:
        raise LookupError("scheduled plan no longer exists")
    from_plan = await control_db.get(Plan, subscription.plan_id)
    effective_at = subscription.pending_plan_effective_at or now
    previous_status = subscription.status
    details = {
        "from_plan": from_plan.code if from_plan else None,
        "to_plan": pending_plan.code,
        "scheduled": True,
        "effective_at": effective_at.isoformat(),
        "proration_amount": str(Decimal("0.00")),
    }
    if subscription.external_subscription_id is not None:
        details["ended_external_subscription_id"] = subscription.external_subscription_id
    if previous_status != "active":
        details["from_status"] = previous_status
        details["to_status"] = "active"

    # The (org, plan) unique constraint: a leftover canceled row on the free
    # plan would collide with the repoint (`plan_catalog`).
    await clear_stale_canceled_subscription(
        control_db, organization_id=subscription.organization_id, plan_id=pending_plan.id
    )
    subscription.plan_id = pending_plan.id
    subscription.status = "active"
    subscription.external_subscription_id = None
    subscription.current_period_start = effective_at
    subscription.current_period_end = add_months(effective_at, 1)
    clear_pending(subscription)
    return details


async def pending_plan_view(control_db: AsyncSession, subscription: Subscription) -> Plan | None:
    """The plan a pending move goes to, for display; None when none is pending."""
    if subscription.pending_plan_id is None:
        return None
    return (
        await control_db.execute(select(Plan).where(Plan.id == subscription.pending_plan_id))
    ).scalar_one_or_none()
