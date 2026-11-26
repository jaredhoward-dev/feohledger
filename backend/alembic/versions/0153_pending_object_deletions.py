"""Queue a deleted invoice's stored documents for deletion (tenant).

Why
---
``docs/decisions.md`` §592. Deleting an invoice removed its rows but left its
documents in object storage for ever: the uploaded file (and every superseded
version §363 kept), a received PEPPOL payload, and its supplier-chat
attachments. ``pending_object_deletions`` is the outbox the delete transaction
writes one row per folder into, so the objects go once the delete commits and a
failure stays a durable to-do for the retention sweep.

Nothing is backfilled: the folders of invoices deleted before this revision are
reached by the retention sweep's orphan scan, which compares the bucket against
live rows, not by a row here.

TENANT DB ONLY: gated on ``invoices`` existing, so a no-op on the control plane;
fanned out by ``scripts/migrate_all_tenants.py``. A fresh tenant gets the table
from ``create_all``, so the DDL mirrors
``app/models/pending_object_deletion.PendingObjectDeletion``. Idempotent
throughout.

Revision ID: 0153_pending_object_deletions
Revises: 0148_card_transaction_events
Create Date: 2026-11-25

29 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id. Round 51 was cut from ``main`` before rounds 48–50's 0149–0152
landed; when the two meet, ``down_revision`` becomes
``0152_peppol_inbound_detach`` so the chain keeps one head.
"""

from sqlalchemy import text

from alembic import op

revision = "0153_pending_object_deletions"
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
    if not _has_table("invoices"):
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_object_deletions (
            id UUID PRIMARY KEY,
            prefix VARCHAR(512) NOT NULL,
            owner_type VARCHAR(30) NOT NULL,
            owner_id UUID NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error VARCHAR(100),
            last_attempt_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            organization_id UUID NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pending_object_deletions_organization_id "
        "ON pending_object_deletions (organization_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pending_object_deletions_created_at "
        "ON pending_object_deletions (created_at)"
    )


def downgrade() -> None:
    if not _has_table("invoices"):
        return
    op.execute("DROP TABLE IF EXISTS pending_object_deletions")
