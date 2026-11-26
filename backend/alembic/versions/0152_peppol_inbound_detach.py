"""An inbound PEPPOL transmission outlives its invoice (tenant).

Why
---
``docs/decisions.md`` §586. Deleting an invoice we RECEIVED over PEPPOL used to
delete its inbound ``peppol_transmissions`` row with it, losing the local record
of a received e-invoice. The delete now detaches the row instead: ``invoice_id``
becomes NULL and three new columns say when, by whom and which invoice
(``invoice_deleted_at``, ``invoice_deleted_by_id``, ``deleted_invoice_id``). The
row is then kept under the ``peppol_transmissions`` retention class.

``ck_peppol_detached_inbound`` holds the rule at the data layer: a row with no
invoice must be inbound and stamped. An outbound row is never detached (its
invoice is undeletable while it is live, §580, and a failed one is cleared).

The partial unique index ``uq_peppol_one_live_per_invoice_direction`` is
unchanged: NULLs are distinct in a unique index, so any number of detached
inbound rows coexist.

Revision ID: 0152_peppol_inbound_detach
Revises: 0151_org_is_partner
Create Date: 2026-11-25

26 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT ONLY: gated on ``peppol_transmissions`` existing, so it no-ops on the
control plane. Mirrors ``app.models.peppol_transmission`` so a ``create_all``
tenant matches a migrated one. Idempotent: ``ADD COLUMN IF NOT EXISTS``, ``DROP
NOT NULL`` (a no-op when already nullable), and the constraint is added only
when absent. Nothing is backfilled: no detached row exists before this revision.

The downgrade cannot reattach a detached row (its invoice is gone), and
restoring ``NOT NULL`` over one would fail, so it refuses while any detached row
exists rather than deleting the received records this revision keeps.
"""

from sqlalchemy import text

from alembic import op

revision = "0152_peppol_inbound_detach"
down_revision = "0151_org_is_partner"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_peppol_detached_inbound"
CHECK = "invoice_id IS NOT NULL OR (direction = 'inbound' AND invoice_deleted_at IS NOT NULL)"


def _is_tenant_db() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'peppol_transmissions'"
            )
        ).scalar()
        is not None
    )


def _has_constraint() -> bool:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT 1 FROM pg_constraint WHERE conname = :n "
                "AND conrelid = 'public.peppol_transmissions'::regclass"
            ),
            {"n": CONSTRAINT},
        )
        .scalar()
        is not None
    )


def upgrade() -> None:
    if not _is_tenant_db():
        return
    op.execute(
        "ALTER TABLE peppol_transmissions "
        "ADD COLUMN IF NOT EXISTS invoice_deleted_at TIMESTAMP WITH TIME ZONE, "
        "ADD COLUMN IF NOT EXISTS invoice_deleted_by_id UUID, "
        "ADD COLUMN IF NOT EXISTS deleted_invoice_id UUID"
    )
    op.execute("ALTER TABLE peppol_transmissions ALTER COLUMN invoice_id DROP NOT NULL")
    if not _has_constraint():
        op.execute(f"ALTER TABLE peppol_transmissions ADD CONSTRAINT {CONSTRAINT} CHECK ({CHECK})")


def downgrade() -> None:
    if not _is_tenant_db():
        return
    detached = (
        op.get_bind()
        .execute(text("SELECT count(*) FROM peppol_transmissions WHERE invoice_id IS NULL"))
        .scalar()
    )
    if detached:
        raise RuntimeError(
            f"{detached} inbound PEPPOL transmission(s) outlived a deleted invoice; "
            "0152 cannot be downgraded without deleting received e-invoice records"
        )
    op.execute(f"ALTER TABLE peppol_transmissions DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute("ALTER TABLE peppol_transmissions ALTER COLUMN invoice_id SET NOT NULL")
    op.execute(
        "ALTER TABLE peppol_transmissions "
        "DROP COLUMN IF EXISTS invoice_deleted_at, "
        "DROP COLUMN IF EXISTS invoice_deleted_by_id, "
        "DROP COLUMN IF EXISTS deleted_invoice_id"
    )
