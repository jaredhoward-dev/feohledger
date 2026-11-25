"""When the open AP falls due: the population, its effective due date, and
what "overdue" means.

Three surfaces count or list overdue AP, and they must agree on the rows:

* the cash-flow forecast (`ap_commitments.commitment_rows`), and with it the
  /cfo "Overdue (owed now)" card (decisions §440, §449), the cash-position
  curve, the what-if, the copilot tools and the shortfall sweep;
* `GET /api/dashboard`'s `overdue_count` / `overdue_total` and
  `due_soon_count` / `due_soon_total`, which the mobile dashboard shows;
* `GET /api/invoices?overdue=true` (with `/counts` and `/ids`), the list the
  mobile Overdue card opens (`overdue_invoice_ids`).

All read ONE select, `open_ap_due_population`, so the predicate is stated
once: the population and its effective due date are built there and nowhere
else, and each surface only adds its own window on `effective_due`
(`is_overdue`, `is_due_soon`, the forecast's horizon).
`tests/test_open_ap_due_single_statement.py` fails if a caller rebuilds the
population from the parts.

* **Population.** Committed AP (`OPEN_AP_STATUSES`, approved and still
  owed) plus the in-review pipeline (`IN_REVIEW_STATUSES`), less any invoice
  already paid out (a `completed` payment awaiting its `paid` sync-back,
  `paid_out`). The forecast's `include_pending=false` narrows to the
  committed half; the dashboard, its upcoming list and the /cfo card read
  both.
* **Effective due date.** The invoice's current payment schedule's due date
  (`payment_schedules.LATEST_SCHEDULE_ORDER`, one row per invoice), else the
  invoice's own. NULL when neither exists: such a row cannot be timed, and
  `NULL < today` is not true, so it is never overdue.
* **Overdue.** Effective due date strictly before today (UTC). Due today is
  on time.
* **Due soon.** Effective due date from today through `DUE_SOON_DAYS` out,
  both ends inclusive. Disjoint from overdue by construction.

Two per-row readers keep a population of their own but time each row by the
same effective date, `effective_due_of`: the payment queue (its date,
`is_overdue` and order, off the current schedule it already joins; its
select-all ids through `current_schedule_due`) and the aging bands
(`aging_band` over `current_schedule_due`, on the AP balance's population
so they reconcile with it, F-4). Decisions §565, §569.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import (
    ColumnElement,
    Lateral,
    Select,
    Subquery,
    and_,
    case,
    exists,
    func,
    not_,
    select,
)

from app.models.invoice import Invoice
from app.models.invoice_status import IN_REVIEW_STATUSES, OPEN_AP_STATUSES
from app.models.payment import Payment, PaymentSchedule
from app.services.payment_schedules import LATEST_SCHEDULE_ORDER
from app.services.payment_settlement_record import payment_covered_sql

#: Approved and still owed: the firm AP commitment.
COMMITTED_STATUSES: tuple[str, ...] = tuple(s.value for s in OPEN_AP_STATUSES)
#: Still in review: projected, lower-certainty outflow.
PENDING_STATUSES: tuple[str, ...] = tuple(s.value for s in IN_REVIEW_STATUSES)
#: Everything the business may still have to pay.
OUTFLOW_STATUSES: tuple[str, ...] = COMMITTED_STATUSES + PENDING_STATUSES
#: "Due soon" runs from today through this many days out.
DUE_SOON_DAYS = 7


def latest_schedule_subquery() -> Subquery:
    """ONE schedule row per invoice, newest first, as columns `invoice_id`,
    `sched_due`, `discount_date`, `discount_percent`.

    A bare `outerjoin(PaymentSchedule)` fans an invoice out to one row per
    schedule, which counted an invoice with two schedules twice at its full
    amount. `DISTINCT ON` with `LATEST_SCHEDULE_ORDER` is the same rule the
    discount engine's `latest_schedule` reads, so their idea of an invoice's
    due date and this one cannot diverge.
    """
    return (
        select(
            PaymentSchedule.invoice_id.label("invoice_id"),
            PaymentSchedule.due_date.label("sched_due"),
            PaymentSchedule.discount_date.label("discount_date"),
            PaymentSchedule.discount_percent.label("discount_percent"),
        )
        .distinct(PaymentSchedule.invoice_id)
        .order_by(PaymentSchedule.invoice_id, *LATEST_SCHEDULE_ORDER)
        .subquery()
    )


def effective_due_of(schedule_due: ColumnElement) -> ColumnElement:
    """THE effective-due rule: the current schedule's due date, else the
    invoice's own (NULL if neither). Every other helper here is this applied to
    some way of reaching the current schedule; a reader that already joins the
    current schedule row (the payment queue, `payment_schedules.latest_schedules`)
    passes its `due_date` here rather than looking the schedule up again."""
    return func.coalesce(schedule_due, Invoice.due_date)


def effective_due_date(latest_schedule: Subquery) -> ColumnElement:
    """`effective_due_of` over `latest_schedule_subquery()`, outer-joined on
    `invoice_id`."""
    return effective_due_of(latest_schedule.c.sched_due)


def is_overdue(effective_due: ColumnElement, today: date) -> ColumnElement:
    """Past due as of `today`: strictly before it. NULL (untimeable) is not."""
    return effective_due < today


def is_due_soon(effective_due: ColumnElement, today: date) -> ColumnElement:
    """Falls due today through `DUE_SOON_DAYS` out. NULL (untimeable) is not."""
    return and_(effective_due >= today, effective_due <= today + timedelta(days=DUE_SOON_DAYS))


def paid_out() -> ColumnElement:
    """The invoice carries a `completed` payment that DISCHARGES it: the money
    has left, in full.

    One live payment per invoice (`payments`' partial unique index); between
    its discharge and the `payment_scheduled → paid` sync-back the invoice is
    not owed, and counting it would show a paid bill as overdue and project its
    outflow twice (decisions §565).

    `completed` alone is not that (decisions §569): a run's card leg is
    `completed` the moment the card is MINTED, before the vendor charges it,
    and a short or unstorable settlement is `completed` while the payment sync
    holds the invoice. "Discharges" is `payment_settlement_record.payment_coverage`'s
    `covered`, the verdict the payment sync and an ERP `paid` report act on,
    through its SQL twin `payment_covered_sql`, so an uncharged card or a short
    settlement leaves the invoice owed (at its face amount) on every overdue
    surface and in the forecast. Correlated on `Invoice.id`.
    """
    return exists(
        select(1).where(
            Payment.invoice_id == Invoice.id,
            Payment.status == "completed",
            payment_covered_sql(),
        )
    )


def open_ap_due_population(statuses: Sequence[str] = OUTFLOW_STATUSES) -> Subquery:
    """THE open-AP population, one row per invoice, with its effective due date.

    Columns: `invoice_id`, `effective_due` (`effective_due_date`, NULL when
    untimeable), and the latest schedule's `discount_date` /
    `discount_percent` for the forecast's discount slice.

    `statuses` defaults to the whole outflow population; the forecast's
    `include_pending=false` passes `COMMITTED_STATUSES`. An invoice already
    paid out (`paid_out`) is not owed and is not in it. Every surface named in
    the module docstring joins this on `invoice_id` (inner join: the status
    filter lives here) and applies only its own window and its own entity
    scope to the outer `Invoice`, so none of them restates the population.

    Unscoped and uncorrelated on purpose: it is a derived table, and the
    caller's `apply_entity_scope` on the outer query narrows it.
    """
    latest = latest_schedule_subquery()
    return (
        select(
            Invoice.id.label("invoice_id"),
            effective_due_date(latest).label("effective_due"),
            latest.c.discount_date.label("discount_date"),
            latest.c.discount_percent.label("discount_percent"),
        )
        .outerjoin(latest, latest.c.invoice_id == Invoice.id)
        .where(Invoice.status.in_(tuple(statuses)), not_(paid_out()))
        .correlate(None)
        .subquery("open_ap_due")
    )


def current_schedule_due() -> Lateral:
    """The outer invoice row's CURRENT schedule due date, as a LATERAL subquery
    with one column, `sched_due`: at most one row (`LATEST_SCHEDULE_ORDER`,
    `LIMIT 1`), none when the invoice has no schedule. Outer-join it
    `ON true` after `invoices` and time the row by
    `effective_due_of(lateral.c.sched_due)`.

    For the per-row readers that keep their own population and must still
    time a row exactly as the count does: the select-all ids behind the payment
    queue and the aging bands, whose population is the AP balance's (F-4).

    Lateral, correlated on the outer `Invoice`, on purpose (decisions §569): it
    used to be an uncorrelated `invoices LEFT JOIN DISTINCT ON (all schedules)`,
    which Postgres cannot narrow by the caller's status filter or LIMIT, so
    every aging call and queue page sorted every schedule in the tenant and
    scanned `invoices` a second time. Correlated, it is one probe of
    `ix_payment_schedules_invoice_current` per invoice the caller's own WHERE
    keeps, and the `LIMIT` keeps the planner from inlining it.
    """
    return (
        select(PaymentSchedule.due_date.label("sched_due"))
        .where(PaymentSchedule.invoice_id == Invoice.id)
        .order_by(*LATEST_SCHEDULE_ORDER)
        .limit(1)
        .lateral("current_schedule_due")
    )


#: The aging bands, in order: not yet due, then days past due to 30 / 60 / 90
#: (inclusive upper edges) and beyond.
AGING_BANDS: tuple[str, ...] = ("current", "days_30", "days_60", "days_90", "days_90_plus")


def aging_band(effective_due: ColumnElement, today: date) -> ColumnElement:
    """The aging band of `effective_due` as of `today`. NULL (untimeable) and
    due today or later are `current`, the same rows `is_overdue` leaves out,
    so a row in a past-due band is exactly a row the overdue count counts."""
    days_past = today - effective_due
    return case(
        (effective_due.is_(None), "current"),
        (days_past <= 0, "current"),
        (days_past <= 30, "days_30"),
        (days_past <= 60, "days_60"),
        (days_past <= 90, "days_90"),
        else_="days_90_plus",
    )


def overdue_invoice_ids(today: date) -> Select:
    """The ids of every overdue invoice as of `today`, over the whole book.

    `open_ap_due_population` filtered by `is_overdue`: the set
    `GET /api/dashboard`'s `overdue_count` counts and the /cfo card's overdue
    figure counts. `GET /api/invoices?overdue=true` filters on it, so the
    mobile Overdue card opens a list with exactly the rows it counted.

    Uncorrelated (`correlate(None)`): a caller composes it as
    `Invoice.id.in_(...)` inside a query on `Invoice`, where SQLAlchemy would
    otherwise correlate the inner `Invoice` to the outer one and drop it from
    the subquery's FROM. The caller's own scope (entity, filters) narrows the
    outer query; this set is deliberately unscoped.
    """
    population = open_ap_due_population()
    return (
        select(population.c.invoice_id)
        .where(is_overdue(population.c.effective_due, today))
        .correlate(None)
    )
