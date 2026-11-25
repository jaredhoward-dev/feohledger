"""Add ``vendor_user_legal_acceptances`` (tenant).

Why
---
``docs/decisions.md`` §499. §495 made employee signup and a first password set
record acceptance of our Terms and Privacy Policy in the control-plane
``legal_acceptances``, keyed by the employee ``user_id``. A supplier-portal
user is a tenant ``VendorUser`` with no control-plane id, so their set-password
link recorded nothing. This table is the append-only record of which version
of the Privacy Policy each portal user acknowledged, when, and through which
control. Only the Privacy Policy: the Terms are a contract with the customer
organisation, which answers for its Supplier Users (§499).

TENANT DB ONLY: gated on ``vendor_users`` existing, so a no-op on the control
plane; fanned out by ``scripts/migrate_all_tenants.py``. A fresh tenant gets
the table from ``create_all`` (it is not in ``CONTROL_TABLES``), so the DDL
here mirrors ``app/models/vendor_user_legal_acceptance.py`` exactly — no
foreign key, by design: the row is kept through a DSAR erasure of the vendor
user and a portal-user delete.

Nothing is backfilled: no supplier acknowledged anything before this, and an
invented row would be false evidence.

Idempotent (``IF NOT EXISTS``).

Revision ID: 0144_vendor_user_legal_accept
Revises: 0143_legal_acceptances
Create Date: 2026-11-24

29 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0144_vendor_user_legal_accept"
down_revision = "0143_legal_acceptances"
branch_labels = None
depends_on = None


def _is_tenant() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'vendor_users'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_tenant():
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vendor_user_legal_acceptances (
            id uuid NOT NULL,
            vendor_user_id uuid NOT NULL,
            document varchar(32) NOT NULL,
            version varchar(32) NOT NULL,
            accepted_at timestamptz NOT NULL DEFAULT now(),
            source varchar(32) NOT NULL,
            CONSTRAINT vendor_user_legal_acceptances_pkey PRIMARY KEY (id),
            CONSTRAINT uq_vendor_user_legal_acceptances_doc_version
                UNIQUE (vendor_user_id, document, version)
        )
        """
    )


def downgrade() -> None:
    """No rollback, deliberately.

    Each row is the evidence that a supplier was shown our Privacy Policy. The
    older code does not read the table, so leaving it costs nothing; drop it by
    hand if ever needed.
    """
