"""Pre-approval cover: which approved estimate covers an expense line, and how
much of it is left (decisions §563).

A pre-approval is a running budget bounded by its ``estimated_amount``. Report
submit links each line that needs cover to ONE pre-approval and records what it
draws (``expenses.preapproval_id`` / ``preapproval_drawn_amount``); the
remaining balance is the estimate minus every recorded draw. Before this, the
largest matching estimate covered each line on its own, so one approval cleared
``preapproval_required`` on any number of lines — the split-to-evade shape.

The rules, in one place:

* **Who may be covered** — unchanged: an approved pre-approval of the line's
  report owner, in the line's own entity (strict, no shared rows), in the
  line's own currency (no conversion; a NULL or unusable code covers nothing,
  §473), linked to the line's report or matching its category.
* **What consumes** — a link, and only submit writes one, so the lines that
  draw are the submitted and approved (and reimbursed) ones. Draft lines draw
  nothing: a draft is an intention, and letting it hold balance would let an
  abandoned draft block a real claim. Report reject hands lines back to draft
  and :func:`release_draw` clears their link in the same transaction; a deleted
  line takes its draw with it (the draw is a column on the line).
* **Only lines that need cover draw** — a line under every policy's
  ``requires_preapproval_above`` threshold is not charged to the budget, so a
  run of small receipts cannot exhaust the estimate the large one needs.
* **Whole-line cover** — a line is covered by one pre-approval or not at all;
  it is never split across two estimates. Which estimate is :func:`assign_cover`
  (best-fit decreasing, then a bounded exact search — §570).
* **Audited** — the submit audit row lists every draw and the reject row every
  release (:func:`draw_records`), since the columns themselves are mutable.
* **Concurrency** — :func:`draw_cover_for_lines` locks every candidate
  pre-approval ``FOR UPDATE`` (ordered by id, so two submits never deadlock)
  before reading the drawn sums, so two reports submitted at once cannot both
  draw the last remainder: the second waits and then sees the first's draw.
* **No backfill** — a line submitted before migration 0149 has no link and
  draws nothing; inventing one would manufacture a refusal or an absolution
  (§141).

Money is ``Decimal`` throughout.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense, ExpensePreapproval, ExpenseReport, PreapprovalStatus
from app.services.expense_currency import normalize_currency
from app.tenant import apply_entity_scope

ZERO = Decimal("0")


def _candidates_query(expense: Expense, owner_user_id: uuid.UUID):
    """Approved pre-approvals that may cover ``expense`` (before balance)."""
    conditions = [ExpensePreapproval.expense_report_id == expense.report_id]
    if expense.category is not None:
        conditions.append(ExpensePreapproval.category == expense.category)
    return apply_entity_scope(
        select(ExpensePreapproval), ExpensePreapproval, expense.entity_id
    ).where(
        ExpensePreapproval.status == PreapprovalStatus.approved,
        ExpensePreapproval.requester_user_id == owner_user_id,
        # "" (no usable code on the expense) matches no pre-approval, and a
        # pre-approval with no currency (NULL) is unknown, never USD: NULL
        # compares as no match, so a currency-less pre-approval covers nothing.
        func.upper(func.btrim(ExpensePreapproval.currency))
        == (normalize_currency(expense.currency) or ""),
        or_(*conditions),
    )


def _preference_key(expense: Expense):
    """Deterministic order among candidates: one raised for THIS report first,
    then the oldest decision — the most specific authorization is spent first."""

    def key(p: ExpensePreapproval):
        return (
            0 if p.expense_report_id == expense.report_id else 1,
            p.decided_at.timestamp() if p.decided_at else float("inf"),
            p.created_at.timestamp() if p.created_at else float("inf"),
            str(p.id),
        )

    return key


async def drawn_by_preapproval(
    db: AsyncSession,
    preapproval_ids: Iterable[uuid.UUID],
    *,
    exclude_expense_ids: Iterable[uuid.UUID] = (),
) -> dict[uuid.UUID, Decimal]:
    """Sum of recorded draws per pre-approval (absent key = nothing drawn)."""
    ids = list({i for i in preapproval_ids if i is not None})
    if not ids:
        return {}
    query = (
        select(Expense.preapproval_id, func.sum(Expense.preapproval_drawn_amount))
        .where(Expense.preapproval_id.in_(ids))
        .group_by(Expense.preapproval_id)
    )
    excluded = list(exclude_expense_ids)
    if excluded:
        query = query.where(Expense.id.not_in(excluded))
    rows = (await db.execute(query)).all()
    return {pid: Decimal(total or 0) for pid, total in rows}


def remaining_of(preapproval: ExpensePreapproval, drawn: Decimal | None) -> Decimal:
    """What is left of an estimate after ``drawn`` (never below zero)."""
    left = Decimal(preapproval.estimated_amount) - (drawn or ZERO)
    return left if left > ZERO else ZERO


async def _owner_of(db: AsyncSession, expense: Expense) -> uuid.UUID | None:
    if expense.report_id is None:
        return None
    return (
        await db.execute(
            select(ExpenseReport.employee_user_id).where(ExpenseReport.id == expense.report_id)
        )
    ).scalar_one_or_none()


async def advisory_cover(db: AsyncSession, expense: Expense) -> Decimal | None:
    """The cover the policy engine may count for ``expense`` right now.

    A line that already draws (it is submitted) is covered by its own recorded
    draw. Otherwise it is the largest REMAINING balance among its candidates —
    so a draft line's stored flags say ``preapproval_required`` again once the
    estimate is spent elsewhere. Advisory: submit is the gate, and re-decides
    under a lock. An unattached line has no owner yet, so nothing covers it."""
    if expense.preapproval_id is not None and expense.preapproval_drawn_amount is not None:
        return Decimal(expense.preapproval_drawn_amount)
    owner = await _owner_of(db, expense)
    if owner is None:
        return None
    candidates = list((await db.execute(_candidates_query(expense, owner))).scalars().all())
    if not candidates:
        return None
    drawn = await drawn_by_preapproval(
        db, [p.id for p in candidates], exclude_expense_ids=[expense.id]
    )
    return max(remaining_of(p, drawn.get(p.id)) for p in candidates)


# An exact search is tried only when best-fit leaves a line uncovered, only for
# a report this small, and only for this many search steps — so a pathological
# report costs a bounded amount of work and then falls back to best-fit.
EXACT_SEARCH_MAX_LINES = 8
EXACT_SEARCH_MAX_STEPS = 20_000


def assign_cover(
    lines: list[tuple[uuid.UUID, Decimal, list[uuid.UUID]]],
    remaining: dict[uuid.UUID, Decimal],
    *,
    max_lines: int = EXACT_SEARCH_MAX_LINES,
    max_steps: int = EXACT_SEARCH_MAX_STEPS,
) -> dict[uuid.UUID, uuid.UUID]:
    """Choose one pre-approval per line (§570). Pure and deterministic.

    ``lines`` is ``(line_id, amount, candidate ids in preference order)`` in a
    stable order; ``remaining`` is each candidate's balance. Returns
    ``line_id → preapproval_id`` for the lines that get cover.

    1. **Best-fit decreasing**: lines largest first; each takes the candidate
       that fits with the SMALLEST balance left over, preference order breaking
       ties. A line nothing fits is left out and the rest still get cover.
    2. If that leaves a line out and the report has at most ``max_lines``
       lines needing cover, an **exact search** (depth-first, same orders) looks
       for an assignment covering every line, giving up after ``max_steps``. If
       it finds one, that is the answer; otherwise best-fit's stands — so a
       report refused here has no full assignment, or is too large to search.
    """
    order = sorted(
        range(len(lines)), key=lambda i: (-lines[i][1], i)
    )  # largest first, stable on the caller's order
    rank = {i: {pid: n for n, pid in enumerate(lines[i][2])} for i in range(len(lines))}

    def fits(i: int, left: dict[uuid.UUID, Decimal]) -> list[uuid.UUID]:
        amount = lines[i][1]
        options = [pid for pid in lines[i][2] if pid in left and left[pid] >= amount]
        return sorted(options, key=lambda pid: (left[pid] - amount, rank[i][pid]))

    greedy: dict[uuid.UUID, uuid.UUID] = {}
    left = dict(remaining)
    for i in order:
        options = fits(i, left)
        if options:
            greedy[lines[i][0]] = options[0]
            left[options[0]] -= lines[i][1]
    if len(greedy) == len(lines) or len(lines) > max_lines:
        return greedy

    steps = 0
    chosen: dict[uuid.UUID, uuid.UUID] = {}
    left = dict(remaining)

    def search(pos: int) -> bool:
        nonlocal steps
        if pos == len(order):
            return True
        i = order[pos]
        for pid in fits(i, left):
            steps += 1
            if steps > max_steps:
                return False
            left[pid] -= lines[i][1]
            chosen[lines[i][0]] = pid
            if search(pos + 1):
                return True
            left[pid] += lines[i][1]
            del chosen[lines[i][0]]
        return False

    return dict(chosen) if search(0) else greedy


async def draw_cover_for_lines(
    db: AsyncSession, report: ExpenseReport, lines: list[Expense]
) -> dict[str, Decimal]:
    """Link each of ``lines`` (the ones that need cover) to a pre-approval with
    enough left, record the draw, and return the engine's cover map
    (``str(expense.id) → drawn``). A line no balance can cover is left
    unlinked and absent from the map, so the engine raises
    ``preapproval_required`` for it and the caller's 422 rolls the whole
    transaction back — draws included. Which line goes where is
    :func:`assign_cover`'s choice.

    Every candidate pre-approval is locked ``FOR UPDATE`` in id order before
    any balance is read, so concurrent submits serialise on the shared rows.
    The locked rows are re-read (``populate_existing``) and one no longer
    ``approved`` once the lock is held covers nothing."""
    for line in lines:
        release_draw(line)
    if not lines:
        return {}
    owner = report.employee_user_id
    candidate_ids: set[uuid.UUID] = set()
    per_line: dict[uuid.UUID, list[uuid.UUID]] = {}
    for line in lines:
        ids = list(
            (
                await db.execute(
                    _candidates_query(line, owner).with_only_columns(ExpensePreapproval.id)
                )
            )
            .scalars()
            .all()
        )
        per_line[line.id] = ids
        candidate_ids.update(ids)
    if not candidate_ids:
        return {}

    locked = {
        p.id: p
        for p in (
            await db.execute(
                select(ExpensePreapproval)
                .where(ExpensePreapproval.id.in_(candidate_ids))
                .order_by(ExpensePreapproval.id)
                .with_for_update()
                # Overwrite any copy the identity map already holds: the status
                # and estimate judged below are the ones read under the lock.
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
        # Cancelled (or otherwise decided away) while we waited for the lock.
        if p.status == PreapprovalStatus.approved
    }
    # Read AFTER the lock: under READ COMMITTED this statement sees every draw
    # a concurrent submit committed while we waited. The report's own lines are
    # excluded — their links were just cleared above and are re-decided here.
    drawn = await drawn_by_preapproval(
        db, locked.keys(), exclude_expense_ids=[line.id for line in lines]
    )

    ordered = sorted(
        lines,
        key=lambda e: (e.created_at.timestamp() if e.created_at else 0.0, str(e.id)),
    )
    by_id = {line.id: line for line in ordered}
    assignment = assign_cover(
        [
            (
                line.id,
                Decimal(line.amount),
                [
                    p.id
                    for p in sorted(
                        (locked[i] for i in per_line[line.id] if i in locked),
                        key=_preference_key(line),
                    )
                ],
            )
            for line in ordered
        ],
        {pid: remaining_of(p, drawn.get(pid)) for pid, p in locked.items()},
    )

    cover: dict[str, Decimal] = {}
    for line_id, pid in assignment.items():
        line = by_id[line_id]
        amount = Decimal(line.amount)
        line.preapproval_id = pid
        line.preapproval_drawn_amount = amount
        cover[str(line.id)] = amount
    return cover


def draw_record(expense: Expense) -> dict | None:
    """The audit form of a line's draw, or ``None`` if it draws nothing.
    Amounts are exact Decimal strings (§570)."""
    if expense.preapproval_id is None or expense.preapproval_drawn_amount is None:
        return None
    return {
        "expense_id": str(expense.id),
        "preapproval_id": str(expense.preapproval_id),
        "amount": str(Decimal(expense.preapproval_drawn_amount)),
        # The draw is in the line's own currency, which is the estimate's (a
        # pre-approval in another currency never covers it).
        "currency": normalize_currency(expense.currency),
    }


def draw_records(expenses: Iterable[Expense]) -> list[dict]:
    """Every draw among ``expenses``, ordered by expense id."""
    records = [r for r in (draw_record(e) for e in expenses) if r is not None]
    return sorted(records, key=lambda r: r["expense_id"])


def release_draw(expense: Expense) -> dict | None:
    """Clear a line's draw — it no longer consumes its pre-approval. Returns
    what it held (:func:`draw_record`), for the caller's audit row."""
    record = draw_record(expense)
    expense.preapproval_id = None
    expense.preapproval_drawn_amount = None
    return record
