"""A move to a free plan waits for the end of the paid period.

Why
---
``docs/decisions.md`` §483. A self-serve move from a paid plan to Free used to
cancel the provider subscription at once, crediting the unused part of the
period to a customer balance that a free workspace has no future invoice to
spend it on. It now schedules the cancel for the end of the period: the
subscription stays on its paid plan, with its entitlements, until then. Our row
has to say what it will become and when, so:

* ``subscriptions.pending_plan_id`` — the plan the row moves onto (FK
  ``plans.id``), NULL when nothing is scheduled;
* ``subscriptions.pending_plan_effective_at`` — when it moves.

Both nullable, no default, no backfill: no row has a scheduled change yet.

Placement
---------
CONTROL-PLANE ONLY: ``subscriptions`` and ``plans`` live in the control DB
(``tenant_provisioning.CONTROL_TABLES``). Gated on both tables existing, as
0138 gates its ``users`` half on ``plans`` (a pre-Alembic tenant can hold an
empty copy of a control table, so ``plans`` alone is the discriminator, and
``subscriptions`` must exist to be altered). It no-ops on a tenant DB.

Idempotent: ``ADD COLUMN IF NOT EXISTS``. The foreign key is declared inline,
so Postgres names it ``subscriptions_pending_plan_id_fkey`` — the name
``create_all`` gives the model's unnamed ``ForeignKey("plans.id")``.

Downgrade drops both columns (and with them the constraint).

Revision ID: 0140_subscription_pending_plan
Revises: 0139_sanctions_false_positive
Create Date: 2026-11-21

30 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

from sqlalchemy import text

from alembic import op

revision = "0140_subscription_pending_plan"
down_revision = "0139_sanctions_false_positive"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :name"
            ),
            {"name": name},
        ).scalar()
        is not None
    )


def _is_control_db() -> bool:
    return _table_exists("plans") and _table_exists("subscriptions")


def upgrade() -> None:
    if not _is_control_db():
        return
    op.execute(
        "ALTER TABLE subscriptions "
        "ADD COLUMN IF NOT EXISTS pending_plan_id uuid REFERENCES plans(id)"
    )
    op.execute(
        "ALTER TABLE subscriptions "
        "ADD COLUMN IF NOT EXISTS pending_plan_effective_at timestamp with time zone"
    )


def downgrade() -> None:
    if not _is_control_db():
        return
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS pending_plan_effective_at")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS pending_plan_id")
