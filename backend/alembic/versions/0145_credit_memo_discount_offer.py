"""Link a credit memo to the discount offer it takes (tenant).

Why
---
``docs/decisions.md`` §520. The payment path read an applied credit memo for
exactly an accepted offer's savings as the discount already taken (§250), so a
return credit that happened to equal the savings suppressed the discount and
the settlement then captured the offer. Nothing recorded which memos were the
discount. This adds that record:

* ``credit_memos.discount_offer_id`` — ``uuid``, FK ``discount_offers.id``
  ``ON DELETE SET NULL`` (as ``payments.discount_offer_id``, 0104), indexed;
* ``uq_credit_memos_discount_offer_live`` — at most one non-void memo per
  offer, since one offer is one deduction.

Nothing is backfilled. The only evidence an old memo was the discount is its
amount, and reading the amount is the bug; every pre-0145 memo stays an
ordinary credit (§520 says what that means for an old workaround memo).

TENANT DB ONLY: gated on ``credit_memos`` and ``discount_offers`` existing, so
a no-op on the control plane; fanned out by ``scripts/migrate_all_tenants.py``.
A fresh tenant gets the column and both indexes from ``create_all``, so the DDL
mirrors ``app/models/credit_memo.py`` exactly. Idempotent throughout.

Revision ID: 0145_credit_memo_discount_offer
Revises: 0144_vendor_user_legal_accept
Create Date: 2026-11-25

31 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0145_credit_memo_discount_offer"
down_revision = "0144_vendor_user_legal_accept"
branch_labels = None
depends_on = None

_FK = "credit_memos_discount_offer_id_fkey"


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
    if not (_has_table("credit_memos") and _has_table("discount_offers")):
        return
    op.execute("ALTER TABLE credit_memos ADD COLUMN IF NOT EXISTS discount_offer_id uuid")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{_FK}' AND conrelid = 'credit_memos'::regclass
            ) THEN
                ALTER TABLE credit_memos
                    ADD CONSTRAINT {_FK} FOREIGN KEY (discount_offer_id)
                    REFERENCES discount_offers (id) ON DELETE SET NULL;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_credit_memos_discount_offer_id "
        "ON credit_memos (discount_offer_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_memos_discount_offer_live "
        "ON credit_memos (discount_offer_id) "
        "WHERE discount_offer_id IS NOT NULL AND status <> 'void'"
    )


def downgrade() -> None:
    if not (_has_table("credit_memos") and _has_table("discount_offers")):
        return
    op.execute("DROP INDEX IF EXISTS uq_credit_memos_discount_offer_live")
    op.execute("DROP INDEX IF EXISTS ix_credit_memos_discount_offer_id")
    op.execute(f"ALTER TABLE credit_memos DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute("ALTER TABLE credit_memos DROP COLUMN IF EXISTS discount_offer_id")
