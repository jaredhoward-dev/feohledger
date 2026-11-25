"""The overdue / due-soon rule is written once, in `services/open_ap_due`.

Three surfaces count or list overdue AP — the dashboard's `overdue_*` /
`due_soon_*`, `GET /api/invoices?overdue=true` and the cash-flow forecast
behind the /cfo "Overdue (owed now)" card — and they must agree on the rows.
`tests/test_dashboard_aggregates.py` pins that agreement against Postgres; this
file pins the reason it holds: every one of them reads
`open_ap_due_population`, and nothing outside `open_ap_due` rebuilds the
population from its parts (the status set, the latest schedule, the
effective due date). The dashboard used to: it imported those parts and
assembled its own copy, which agreed row for row only because both copies
happened to be written the same way.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from sqlalchemy import select, true
from sqlalchemy.dialects import postgresql

from app.models.invoice import Invoice
from app.services.open_ap_due import overdue_invoice_ids

APP = Path(__file__).resolve().parents[1] / "app"
OWNER = APP / "services" / "open_ap_due.py"

#: The parts the population is built from. Reading any of them outside the
#: owner is how a second copy of the rule starts.
_POPULATION_PARTS = {"OUTFLOW_STATUSES", "latest_schedule_subquery", "effective_due_date"}

#: Every module that counts or lists overdue / due-soon open AP, and the shared
#: name it must reach the population through.
_READERS = {
    APP / "services" / "dashboard_metrics.py": "open_ap_due_population",
    APP / "services" / "ap_commitments.py": "open_ap_due_population",
    APP / "api" / "invoices.py": "overdue_invoice_ids",
}

#: The per-row readers that keep a population of their own (the payment queue,
#: the aging bands, which reconcile with the AP balance) but must time each row
#: by the same effective due date (decisions §565), and the names they read it
#: through: `effective_due_of` is the rule itself, `current_schedule_due` the
#: correlated way to reach the schedule it reads (decisions §569; the queue's
#: page applies the rule to the current schedule it already joins).
_PER_ROW_READERS = {
    APP / "api" / "payments.py": {"effective_due_of", "current_schedule_due", "is_overdue"},
    APP / "services" / "dashboard_metrics.py": {
        "effective_due_of",
        "current_schedule_due",
        "aging_band",
    },
    APP / "services" / "ap_commitments.py": {
        "effective_due_of",
        "current_schedule_due",
        "aging_band",
    },
}


def _names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name)
    return out


def test_no_module_outside_open_ap_due_rebuilds_the_population():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path == OWNER:
            continue
        hit = _names(ast.parse(path.read_text())) & _POPULATION_PARTS
        if hit:
            offenders.append(f"{path.relative_to(APP.parent)}: {sorted(hit)}")
    assert not offenders, (
        "These modules rebuild the open-AP due population from its parts instead "
        "of reading `open_ap_due.open_ap_due_population` (or `overdue_invoice_ids`):\n"
        + "\n".join(offenders)
    )


def test_every_overdue_surface_reads_the_shared_select():
    missing = [
        f"{path.relative_to(APP.parent)} does not use {name}"
        for path, name in _READERS.items()
        if name not in _names(ast.parse(path.read_text()))
    ]
    assert not missing, "\n".join(missing)


def test_every_per_row_reader_times_rows_by_the_effective_due_date():
    missing = []
    for reader, names in _PER_ROW_READERS.items():
        absent = names - _names(ast.parse(reader.read_text()))
        if absent:
            missing.append(f"{reader.relative_to(APP.parent)} does not use {sorted(absent)}")
    assert not missing, "\n".join(missing)


def test_no_reader_times_a_row_on_the_invoice_due_date_alone():
    """`today - Invoice.due_date` or `Invoice.due_date < today` in a reader is
    how the per-row readings drifted from the count (decisions §565)."""
    offenders = []
    for reader in {*_READERS, *_PER_ROW_READERS}:
        src = reader.read_text()
        for needle in ("today - Invoice.due_date", "Invoice.due_date < today", "due_date < today"):
            if needle in src:
                offenders.append(f"{reader.relative_to(APP.parent)}: {needle!r}")
    assert not offenders, "\n".join(offenders)


def test_overdue_ids_keep_their_own_from_inside_an_invoice_query():
    """Composed as `Invoice.id IN (...)` inside a query on `Invoice`, the set must
    still select from `invoices` itself; correlated to the outer row it would
    collapse to a predicate on that row and stop meaning "the overdue set"."""
    outer = select(Invoice.id).where(Invoice.id.in_(overdue_invoice_ids(date(2026, 6, 1))))
    sql = str(outer.compile(dialect=postgresql.dialect()))
    inner = sql.split(" IN ", 1)[1]
    assert "FROM invoices" in inner
    assert "DISTINCT ON (payment_schedules.invoice_id)" in inner


def test_no_reader_restates_the_effective_due_coalesce():
    """The rule is `coalesce(current schedule's due, invoice's own)`, written
    once in `open_ap_due.effective_due_of`. A reader writing the coalesce
    itself is a second copy that can drift (decisions §569)."""
    offenders = []
    for reader in {*_READERS, *_PER_ROW_READERS}:
        for node in ast.walk(ast.parse(reader.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "coalesce"
                and any(
                    isinstance(a, ast.Attribute)
                    and a.attr == "due_date"
                    and isinstance(a.value, ast.Name)
                    and a.value.id == "Invoice"
                    for a in node.args
                )
            ):
                offenders.append(f"{reader.relative_to(APP.parent)}:{node.lineno}")
    assert not offenders, "\n".join(offenders)


def test_the_per_row_due_lookup_is_correlated_not_a_whole_table_scan():
    """`current_schedule_due` is LATERAL and correlated on the outer invoice:
    an uncorrelated `DISTINCT ON` over every schedule cannot be narrowed by
    the caller's status filter or LIMIT (decisions §569)."""
    from app.services.open_ap_due import current_schedule_due, effective_due_of

    current = current_schedule_due()
    q = (
        select(Invoice.id, effective_due_of(current.c.sched_due))
        .select_from(Invoice)
        .outerjoin(current, true())
        .where(Invoice.status == "approved")
    )
    sql = str(q.compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN LATERAL" in sql
    assert "DISTINCT ON" not in sql
    assert "payment_schedules.invoice_id = invoices.id" in sql
    assert sql.count("FROM invoices") == 1
