"""What a plan change will charge — shown before it is made, held to at confirm.

`POST /api/billing/change-plan` used to charge at once: the customer pressed
"Change plan" (or, worse, just saved a card) and Stripe billed a prorated
amount nobody had been shown. This module is the other half of that change
(decisions §482):

* :func:`preview_plan_change` — the provider's own figures for the change,
  without making it: what is due today (or credited), what recurs and from
  when, and the instant it was priced at (``proration_date``);
* :func:`verify_quote` — run by `plan_change.change_plan` when the customer
  confirms, inside the live change's lease: the quote must be recent
  (:data:`PREVIEW_TTL`, same UTC calendar month) and the provider must, asked
  again with the same ``proration_date``, not want MORE than the customer
  confirmed. Otherwise the change is refused ``plan_change_preview_stale`` and
  the customer reviews the new figure.

For a paid → paid change the confirmed ``proration_date`` is also handed to the
real Stripe update, so the invoice equals the preview to the cent. A new
subscription is created "now" (Stripe has no ``proration_date`` on create), so
its first invoice is the prorated stretch to the 1st as of the create — never
more than the preview, which priced a longer stretch, and the confirm-time
check refuses outright if it would be.

A move from a paid plan to a free one is not priced at the provider at all:
it is scheduled for the end of the paid period (`scheduled_downgrade`,
decisions §483), so the preview answers nothing due, no credit and the
``effective_at`` it takes effect.

The preview is read-only at the provider (``create_preview`` creates nothing)
apart from the provisioning it shares with the change: the org's provider
customer and the plan's prices are resolved-or-created first, because a
provider can only price items that exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.billing import Plan
from app.models.organization import Organization
from app.services.billing.entitlements import get_active_subscription
from app.services.billing.period import current_period
from app.services.billing.provider_sync import (
    ProviderLegRefused,
    calendar_month,
    is_live_provider,
    next_month_start,
    plan_is_paid,
    refuse_unbilled_paid_plan,
)
from app.services.billing.provisioning import (
    ProvisionedIds,
    billing_adapter_for,
    provision_org_billing,
)
from app.services.billing.scheduled_downgrade import (
    estimated_effective_at,
    waits_for_period_end,
)
from app.services.billing_adapters.base import (
    BillingAdapter,
    PlanChangePreviewRequest,
    ProviderPlanChangePreview,
)

#: How long a preview stays confirmable. Long enough to read it and press
#: Confirm; short enough that the figure is still the provider's. Adding a card
#: happens BEFORE the review step (the SPA asks for a fresh preview after it),
#: so this never has to cover a card form.
PREVIEW_TTL = timedelta(minutes=10)

#: A quote stamped a moment AFTER the confirming request's clock (another
#: worker's clock) is tolerated by this much; further in the future is forged.
CLOCK_SKEW = timedelta(seconds=30)

#: The coded refusal for a quote that is missing, too old, from another
#: calendar month, or now cheaper than the provider would charge.
STALE_QUOTE = "plan_change_preview_stale"

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class PlanChangeQuote:
    """What the customer saw and confirmed: the preview's pricing instant and
    the amount due it showed, in its currency."""

    proration_date: datetime
    amount_due: Decimal
    currency: str


@dataclass(frozen=True)
class PlanChangePreview:
    plan_code: str
    #: The org is already on the target plan: confirming is a no-op.
    already_on_plan: bool
    currency: str
    #: Charged when the change is confirmed (never negative).
    amount_due: Decimal
    #: Credited to the customer's balance instead (a downgrade), else 0.00.
    credit: Decimal
    #: What recurs afterwards and from when; None when nothing recurs.
    next_amount: Decimal | None
    next_date: datetime | None
    #: The instant this was priced at; the confirm sends it back.
    proration_date: datetime
    expires_at: datetime
    #: A paid target and no card on file: the change will be refused until one
    #: is saved. The figures are still shown; the SPA asks for the card first.
    payment_method_required: bool
    #: When the move takes effect, if not on confirm: a move to a free plan
    #: keeps the paid plan until its period ends (decisions §483). None = now.
    effective_at: datetime | None = None
    #: Confirming cancels the org's pending move to a free plan.
    clears_scheduled_change: bool = False


def _seconds(dt: datetime) -> datetime:
    """Whole seconds, UTC: the provider's ``proration_date`` is Unix seconds,
    and the quote round-trips through the browser as one."""
    return dt.astimezone(UTC).replace(microsecond=0)


async def _target_plan(control_db: AsyncSession, code: str) -> Plan:
    # Imported here: `plan_change` imports this module.
    from app.services.billing.plan_change import PlanChangeError

    plan = (
        await control_db.execute(select(Plan).where(Plan.code == code, Plan.is_active.is_(True)))
    ).scalar_one_or_none()
    if plan is None:
        raise PlanChangeError("unknown_plan")
    return plan


def pricing_window(subscription, *, live: bool, at: datetime) -> tuple[datetime, datetime]:
    """The window a change at ``at`` prorates over: the UTC calendar month with
    a live provider (every live subscription is anchored on the 1st), else the
    row's own period — the same windows `change_plan` records with. Computed
    before provisioning, whose commit expires the loaded row."""
    if live:
        return calendar_month(at)
    period = current_period(subscription, now=at)
    return period.start, period.end


async def price_change(
    adapter: BillingAdapter,
    *,
    live: bool,
    ids: ProvisionedIds,
    external_subscription_id: str | None,
    old_monthly_price: Decimal,
    new_plan: Plan,
    window: tuple[datetime, datetime],
    at: datetime,
    include_recurring: bool,
) -> ProviderPlanChangePreview:
    """Ask the provider what moving onto ``new_plan`` at ``at`` would charge.

    Resolves the provider subscription the way `provider_sync.apply_plan_at_provider`
    will act on it — the row's id, else a live one a dead attempt left on the
    customer (which the change adopts and amends) — so the preview prices the
    call the change will actually make. Refuses ``billing_not_configured`` when
    the adapter cannot price a change ahead of time.
    """
    to_paid = plan_is_paid(new_plan)
    sub_id = external_subscription_id
    anchor = None
    if live:
        if sub_id is None:
            found = await adapter.find_live_subscription(ids.customer_id)
            sub_id = found.external_subscription_id if found else None
        if to_paid and sub_id is None:
            anchor = next_month_start(at)
    else:
        # The mock prices our own row; it has no provider subscription.
        sub_id = None
    result = await adapter.preview_plan_change(
        PlanChangePreviewRequest(
            new_plan_code=new_plan.code,
            to_paid=to_paid,
            old_monthly_price=old_monthly_price,
            new_monthly_price=new_plan.monthly_price,
            currency=new_plan.currency,
            proration_date=at,
            period_start=window[0],
            period_end=window[1],
            customer_id=ids.customer_id,
            external_subscription_id=sub_id,
            price_id=ids.price_id,
            overage_price_id=ids.overage_price_id,
            billing_cycle_anchor=anchor,
            include_recurring=include_recurring,
        )
    )
    if result is None:
        raise ProviderLegRefused("billing_not_configured")
    return result


async def preview_plan_change(
    control_db: AsyncSession,
    *,
    org: Organization,
    new_plan_code: str,
    now: datetime | None = None,
) -> PlanChangePreview:
    """The provider's figures for moving the org onto ``new_plan_code`` now.

    Same refusals as `change_plan` up to the charge itself: 404-class
    :class:`PlanChangeError` (unknown plan, no live subscription) and the coded
    :class:`ProviderLegRefused` (``billing_not_configured`` — a deployed
    environment on ``mock``, a ``mock`` org whose row Stripe bills, an adapter
    that cannot preview). A missing card is NOT a refusal here: the preview
    says so (``payment_method_required``) and the SPA asks for one first.
    """
    from app.services.billing.plan_change import PlanChangeError

    at = _seconds(now or datetime.now(UTC))
    new_plan = await _target_plan(control_db, new_plan_code)
    active = await get_active_subscription(control_db, org.id)
    if active is None:
        raise PlanChangeError("no_live_subscription")
    subscription, current = active
    if current.id == new_plan.id:
        return PlanChangePreview(
            plan_code=new_plan.code,
            already_on_plan=True,
            currency=new_plan.currency,
            amount_due=ZERO,
            credit=ZERO,
            next_amount=None,
            next_date=None,
            proration_date=at,
            expires_at=at + PREVIEW_TTL,
            payment_method_required=False,
        )

    adapter = billing_adapter_for(org)
    live = is_live_provider(adapter)
    refuse_unbilled_paid_plan(adapter, new_plan, deployed=settings.is_deployed)
    external_id = subscription.external_subscription_id
    if not live and external_id is not None:
        raise ProviderLegRefused("billing_not_configured")
    if waits_for_period_end(subscription, current, new_plan, live=live):
        # Kept until the period ends, then Free: nothing is charged today and
        # nothing is credited — no provider call is needed to say so.
        return PlanChangePreview(
            plan_code=new_plan.code,
            already_on_plan=False,
            currency=current.currency,
            amount_due=ZERO,
            credit=ZERO,
            next_amount=None,
            next_date=None,
            proration_date=at,
            expires_at=at + PREVIEW_TTL,
            payment_method_required=False,
            effective_at=(
                subscription.pending_plan_effective_at
                if subscription.pending_plan_id == new_plan.id
                and subscription.pending_plan_effective_at is not None
                else estimated_effective_at(subscription, live=live, now=at)
            ),
        )
    clears_pending = subscription.pending_plan_id is not None
    window = pricing_window(subscription, live=live, at=at)
    old_price = current.monthly_price

    ids = await provision_org_billing(control_db, org=org, plan=new_plan)
    figures = await price_change(
        adapter,
        live=live,
        ids=ids,
        external_subscription_id=external_id,
        old_monthly_price=old_price,
        new_plan=new_plan,
        window=window,
        at=at,
        include_recurring=True,
    )
    card_needed = (
        live
        and plan_is_paid(new_plan)
        and not await adapter.ensure_default_payment_method(ids.customer_id)
    )
    return PlanChangePreview(
        plan_code=new_plan.code,
        already_on_plan=False,
        currency=figures.currency,
        amount_due=figures.amount_due,
        credit=-figures.total if figures.total < ZERO else ZERO,
        next_amount=figures.next_amount,
        next_date=figures.next_date,
        proration_date=at,
        expires_at=at + PREVIEW_TTL,
        payment_method_required=card_needed,
        clears_scheduled_change=clears_pending,
    )


def quote_is_current(quote: PlanChangeQuote | None, *, now: datetime, live: bool) -> bool:
    """Whether ``quote`` may still be confirmed at ``now``: priced no more than
    :data:`PREVIEW_TTL` ago (and not in the future), and — with a live
    provider — in the same UTC calendar month, since the month decides the
    anchor a new subscription gets and the period ``proration_date`` must fall
    inside."""
    if quote is None:
        return False
    age = now - quote.proration_date
    if age < -CLOCK_SKEW or age > PREVIEW_TTL:
        return False
    return not live or calendar_month(quote.proration_date) == calendar_month(now)


def quote_matches(quote: PlanChangeQuote, fresh: ProviderPlanChangePreview) -> bool:
    """The provider, asked again, wants no more than the customer confirmed,
    in the same currency. Less is allowed: a new subscription created a moment
    after its preview prorates a slightly shorter stretch.

    What this does not hold (decisions §482): a downgrade's CREDIT is not part
    of the quote — nothing is charged, so a smaller credit re-priced at confirm
    is accepted; AI-read overage accrued after this check (reported meter
    events, which Stripe aggregates asynchronously) is billed on the change's
    invoice on top; and the customer's Stripe balance can move between this
    check and the charge. The quote is the client's own statement of what it
    showed — the SPA sends the preview back unchanged."""
    return fresh.currency.upper() == quote.currency.upper() and fresh.amount_due <= quote.amount_due
