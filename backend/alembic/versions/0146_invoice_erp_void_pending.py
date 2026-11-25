"""Record an ERP bill void in progress on the invoice (tenant).

Why
---
``docs/decisions.md`` §545. ``POST /api/exceptions/{id}/void-erp-bill`` used to
hold the invoice, flag and workflow-instance row locks across its two ERP
calls (a status read and the void, up to ~105 s together). It now commits a
"void in progress" marker under the locks, makes the ERP calls with no lock
held, then re-locks and finishes. This column is that marker:

* ``invoices.erp_void_pending`` — ``jsonb``, nullable: ``{attempt_id,
  exception_id, document_id, actor_id, reason, started_at}`` while an attempt
  is in flight, SQL NULL otherwise.

It is on the invoice, not in the workflow instance's ``state_data``, because
the invoice row is the one every writer of the invoice (and the flag's clear)
locks or reads first, so a check of it under that lock is race-free.

Nothing is backfilled: no attempt is in flight across a deploy that predates
the column.

TENANT DB ONLY: gated on ``invoices`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. A fresh tenant gets the
column from ``create_all``, so the DDL mirrors ``app/models/invoice.py``.
Idempotent throughout.

Revision ID: 0146_invoice_erp_void_pending
Revises: 0145_credit_memo_discount_offer
Create Date: 2026-11-25

29 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0146_invoice_erp_void_pending"
down_revision = "0145_credit_memo_discount_offer"
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
    if not _has_table("invoices"):
        return
    op.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS erp_void_pending jsonb")


def downgrade() -> None:
    if not _has_table("invoices"):
        return
    op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS erp_void_pending")
