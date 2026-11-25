"""Add ``legal_acceptances`` (control plane).

Why
---
``docs/decisions.md`` §495. `/legal/terms` says the Terms are accepted "by
clicking an accept" control, and the Xero developer terms require users to
agree to our own Terms and Privacy Policy, but signup recorded nothing. This
table is the append-only record of who accepted which version of which
document, when, and through which control (signup, set-password, re-prompt).

CONTROL PLANE ONLY: gated on ``organizations`` existing, so a no-op on every
tenant DB (``migrate_all_tenants.py`` runs it there harmlessly). A fresh
control plane gets the table from ``create_all`` (``CONTROL_TABLES``), so the
DDL here mirrors ``app/models/legal_acceptance.py`` exactly — no foreign key,
by design: the row is evidence of a contract and outlives an erased user or a
deleted workspace.

Nothing is backfilled: no user accepted anything before this, and inventing an
acceptance would be false evidence. Every existing user meets the re-prompt on
their next sign-in.

Idempotent (``IF NOT EXISTS``).

Revision ID: 0143_legal_acceptances
Revises: 0142_closure_operator_origin
Create Date: 2026-11-24

22 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

from sqlalchemy import text

from alembic import op

revision = "0143_legal_acceptances"
down_revision = "0142_closure_operator_origin"
branch_labels = None
depends_on = None


def _is_control_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'organizations'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_control_db():
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS legal_acceptances (
            id uuid NOT NULL,
            user_id uuid NOT NULL,
            organization_id uuid NOT NULL,
            document varchar(32) NOT NULL,
            version varchar(32) NOT NULL,
            accepted_at timestamptz NOT NULL DEFAULT now(),
            source varchar(32) NOT NULL,
            CONSTRAINT legal_acceptances_pkey PRIMARY KEY (id),
            CONSTRAINT uq_legal_acceptances_user_doc_version UNIQUE (user_id, document, version)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_legal_acceptances_user_id ON legal_acceptances (user_id)"
    )


def downgrade() -> None:
    """No rollback, deliberately.

    Each row is the evidence that a user accepted our Terms and Privacy Policy.
    The older code does not read the table, so leaving it costs nothing; drop
    it by hand if ever needed.
    """
