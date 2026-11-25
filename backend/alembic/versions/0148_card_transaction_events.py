"""Remember card transaction events across deliveries (tenant).

Why
---
``docs/decisions.md`` §554. Two card-webhook facts outlive the delivery that
carries them:

* A reversal or expiry delivered BEFORE its authorization finds no hold to
  release; when the authorization of that same provider transaction arrives,
  it must be applied as already lapsed instead of charging the card.
* Each further clearing on a settled card (an incremental capture) adds to
  what the card was charged, and must be added exactly once.

``card_transaction_events`` holds both, unique per card by
``(kind, event_key)``. The ``card.*`` audit rows are not used instead: in
``lambda`` audit mode they ship to SQS and may not be in the tenant database.
Nothing is backfilled: no earlier delivery was remembered, so there is nothing
honest to recover.

TENANT DB ONLY: gated on ``virtual_cards`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. A fresh tenant gets the
table from ``create_all``, so the DDL mirrors
``app/models/virtual_card.CardTransactionEvent``. Idempotent throughout.

Revision ID: 0148_card_transaction_events
Revises: 0147_card_first_authorized_at
Create Date: 2026-11-25

28 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0148_card_transaction_events"
down_revision = "0147_card_first_authorized_at"
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
    if not _has_table("virtual_cards"):
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS card_transaction_events (
            id UUID PRIMARY KEY,
            virtual_card_id UUID NOT NULL REFERENCES virtual_cards(id) ON DELETE CASCADE,
            kind VARCHAR(20) NOT NULL,
            event_key VARCHAR(255) NOT NULL,
            provider_transaction_id VARCHAR(255),
            lapse VARCHAR(20),
            amount NUMERIC(15, 2),
            currency VARCHAR(3),
            organization_id UUID NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_card_transaction_events_card_kind_key "
        "ON card_transaction_events (virtual_card_id, kind, event_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_card_transaction_events_organization_id "
        "ON card_transaction_events (organization_id)"
    )


def downgrade() -> None:
    if not _has_table("virtual_cards"):
        return
    op.execute("DROP TABLE IF EXISTS card_transaction_events")
