"""Draw expense lines down against the pre-approval that covers them (tenant).

Why
---
``docs/decisions.md`` §563. An approved pre-approval was never consumed: the
largest matching estimate covered each line on its own, so one "500 USD,
travel" approval cleared ``preapproval_required`` on any number of 400 USD
lines across any number of reports. Report submit now links each line that
needs cover to one pre-approval and records how much it draws, under a row
lock on the pre-approval, so the lines it covers never sum past its estimate:

* ``expenses.preapproval_id`` — UUID, nullable, FK ``expense_preapprovals.id``,
  indexed (the remaining-balance sum groups by it).
* ``expenses.preapproval_drawn_amount`` — ``NUMERIC(15, 2)``, nullable; the
  amount drawn, in the pre-approval's currency.

Nothing is backfilled. A line submitted before this revision has no recorded
cover, and choosing one for it now would be a proxy: it would either refuse a
later line on a draw nobody made, or absolve one with a balance nobody checked
(§141). Pre-existing lines therefore draw nothing, and every approved
pre-approval starts with its whole estimate remaining.

TENANT DB ONLY: gated on ``expenses`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. A fresh tenant gets
the columns from ``create_all``, so the DDL mirrors ``app/models/expense.py``.
Idempotent throughout.

Revision ID: 0149_expense_preapproval_draw
Revises: 0148_card_transaction_events
Create Date: 2026-11-25

29 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0149_expense_preapproval_draw"
down_revision = "0148_card_transaction_events"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": name},
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _has_table("expenses") or not _has_table("expense_preapprovals"):
        return
    op.execute("ALTER TABLE expenses ADD COLUMN IF NOT EXISTS preapproval_id UUID")
    op.execute(
        "ALTER TABLE expenses ADD COLUMN IF NOT EXISTS preapproval_drawn_amount NUMERIC(15, 2)"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'expenses_preapproval_id_fkey'
                  AND conrelid = 'expenses'::regclass
            ) THEN
                ALTER TABLE expenses ADD CONSTRAINT expenses_preapproval_id_fkey
                    FOREIGN KEY (preapproval_id) REFERENCES expense_preapprovals(id);
            END IF;
        END $$;
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_expenses_preapproval_id ON expenses (preapproval_id)")


def downgrade() -> None:
    if not _has_table("expenses"):
        return
    op.execute("DROP INDEX IF EXISTS ix_expenses_preapproval_id")
    op.execute("ALTER TABLE expenses DROP CONSTRAINT IF EXISTS expenses_preapproval_id_fkey")
    op.execute("ALTER TABLE expenses DROP COLUMN IF EXISTS preapproval_drawn_amount")
    op.execute("ALTER TABLE expenses DROP COLUMN IF EXISTS preapproval_id")
