"""Record when a virtual card was first authorized (tenant).

Why
---
``docs/decisions.md`` §549. Since §544 a card whose authorization is reversed
or expires returns to ``active`` with ``charged_at`` cleared, so nothing on the
row says it was ever used. A single-use card (Lithic's ``SINGLE_USE``) is closed
at the provider by its first authorization, so a re-run converging a payment
onto it would wait for a charge nobody can make. This column is the durable
fact ``card_issuance.card_settlement_block`` refuses on
(``card_authorized_before``):

* ``virtual_cards.first_authorized_at`` — ``timestamptz``, nullable; set by the
  card webhook's first authorization, never cleared.

The ``card.authorization_lapsed`` audit row is not used instead: in ``lambda``
audit mode it ships to SQS and may not be in the tenant database.

Backfill: a card with ``charged_at`` set was authorized at that moment, so it
is copied. A card whose hold already lapsed under §544 has no ``charged_at``
left; §544 has not shipped (it is in the same unmerged round), so no deployed
row is in that state.

TENANT DB ONLY: gated on ``virtual_cards`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. A fresh tenant gets the
column from ``create_all``, so the DDL mirrors ``app/models/virtual_card.py``.
Idempotent throughout.

Revision ID: 0147_card_first_authorized_at
Revises: 0146_invoice_erp_void_pending
Create Date: 2026-11-25

29 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0147_card_first_authorized_at"
down_revision = "0146_invoice_erp_void_pending"
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
        "ALTER TABLE virtual_cards ADD COLUMN IF NOT EXISTS first_authorized_at "
        "TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "UPDATE virtual_cards SET first_authorized_at = charged_at "
        "WHERE first_authorized_at IS NULL AND charged_at IS NOT NULL"
    )


def downgrade() -> None:
    if not _has_table("virtual_cards"):
        return
    op.execute("ALTER TABLE virtual_cards DROP COLUMN IF EXISTS first_authorized_at")
