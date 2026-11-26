"""Index ``payment_schedules`` in current-schedule order, per invoice.

Why
---
``docs/decisions.md`` §569. An invoice's current payment schedule is its
newest row by ``LATEST_SCHEDULE_ORDER`` (``created_at DESC, id DESC``). The
per-row effective-due readers (``open_ap_due.current_schedule_due``: the aging
bands and the payment queue's select-all ids) now probe it once per invoice in
play, and the remaining ``DISTINCT ON (invoice_id)`` readers
(``payment_schedules.latest_schedules``, ``open_ap_due.latest_schedule_subquery``)
take it in that order. On ``ix_payment_schedules_invoice_id`` alone each probe
re-sorted the invoice's rows and each ``DISTINCT ON`` sorted the whole table;
on ``(invoice_id, created_at DESC, id DESC)`` both read the index in order.

The single-column index stays: it backs the ``invoice_id`` foreign key's
lookups and is ``index=True`` on the model, so dropping it would split fresh
and migrated tenants.

Revision ID: 0150_pay_schedule_current_idx
Revises: 0149_expense_preapproval_draw
Create Date: 2026-11-25

29 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``payment_schedules`` existing, so a no-op on the
control plane; fanned out by ``scripts/migrate_all_tenants.py``. Declared as
an ``Index`` in ``PaymentSchedule.__table_args__`` so a fresh tenant's
``create_all`` builds the same index under the same name. Idempotent +
reversible: ``CREATE INDEX IF NOT EXISTS`` / ``DROP INDEX IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0150_pay_schedule_current_idx"
down_revision = "0149_expense_preapproval_draw"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_payment_schedules_invoice_current"


def _is_tenant_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'payment_schedules'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        "ON payment_schedules (invoice_id, created_at DESC, id DESC)"
    )


def downgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
