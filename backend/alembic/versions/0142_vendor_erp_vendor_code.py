"""The ERP's number for a vendor gets its own column, out of our ``code``.

Why
---
``docs/decisions.md`` §490. The vendor sync wrote each ERP record's display
number (BC No., NetSuite ``entityid``, Intacct ``VENDORID``, SYSPRO
``Supplier``, ...) into ``vendors.code``, the column a person types a supplier
code into and the one CSV import resolves an invoice row's vendor by
(``services/csv_import``). An ERP number equal to a code a person chose for a
different supplier, or to the code an older system's export carries, attached
the CSV row, and so the payable, to the wrong payee.

This revision:

1. Adds ``vendors.erp_vendor_code VARCHAR(50)``, which the sync writes from now
   on and nothing resolves a vendor by.
2. Moves ``code`` into it, leaving ``code`` NULL, on the rows the sync itself
   created: ``source = 'erp_sync' AND onboarded_by_user_id IS NULL``. The sync
   rewrote ``code`` on every pull, so on those rows it is the ERP's. Every
   other row keeps its ``code`` untouched: a person's vendor that the sync
   later linked by name may hold a code the sync overwrote, but nothing here
   can tell that from one the person typed, so its first sync after this
   revision decides by evidence instead (``code`` equal to the ERP's number
   while ``erp_vendor_code`` is still empty moves it; ``services/vendor_sync``).

Nothing is reassigned: no invoice's ``vendor_id`` changes. A CSV row naming a
moved number is refused (``csv_vendor_code_is_erp_number``), never guessed.

Placement
---------
TENANT DB ONLY: gated on ``vendors`` and ``vendor_erp_links`` (0125, tenant
only) existing, so a no-op on the control plane; fanned out by
``scripts/migrate_all_tenants.py``.

Idempotent: ``ADD COLUMN IF NOT EXISTS``, and the move only touches rows whose
``erp_vendor_code`` is still NULL. Downgrade copies the number back into an
empty ``code`` and drops the column.

Revision ID: 0142_vendor_erp_vendor_code
Revises: 0141_vendor_erp_link_unique
Create Date: 2026-11-22

27 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0142_vendor_erp_vendor_code"
down_revision = "0141_vendor_erp_link_unique"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return (
        op.get_bind()
        .execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": name},
        )
        .scalar()
        is not None
    )


def _is_tenant() -> bool:
    return _has_table("vendors") and _has_table("vendor_erp_links")


def upgrade() -> None:
    if not _is_tenant():
        return
    op.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS erp_vendor_code VARCHAR(50)")
    op.execute(
        "UPDATE vendors SET erp_vendor_code = code, code = NULL "
        "WHERE source = 'erp_sync' AND onboarded_by_user_id IS NULL "
        "AND code IS NOT NULL AND erp_vendor_code IS NULL"
    )


def downgrade() -> None:
    if not _is_tenant():
        return
    op.execute(
        "UPDATE vendors SET code = erp_vendor_code "
        "WHERE code IS NULL AND erp_vendor_code IS NOT NULL"
    )
    op.execute("ALTER TABLE vendors DROP COLUMN IF EXISTS erp_vendor_code")
