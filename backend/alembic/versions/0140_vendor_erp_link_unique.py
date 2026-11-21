"""One vendor per ERP record per company: a unique index on the vendor's link.

Why
---
``docs/decisions.md`` §484. ``vendors.erp_vendor_id`` had no unique index, so
two vendors could hold one ERP record's id in one company. The vendor sync
looks a pulled record up by that id (``services/vendor_sync``) with
``scalar_one_or_none``, which raises on two rows, so one such pair aborted
every vendor sync the tenant ran; the ERP push would also post two payees'
bills to one supplier.

This revision:

1. **Refuses** while two or more vendors hold the same ``erp_vendor_id`` under
   the same ``erp_link_realm`` (a NULL realm counting as one company, as the
   index below treats it). Which vendor keeps the link is an operator call,
   not something a migration may guess: merging the vendors releases the
   duplicate link (decisions §373), or unlinking the stale one clears it
   (``POST /api/vendors/{id}/erp-links/unlink``, §375). The error names the
   database, the counts and the vendors' row ids (at most ten), never a
   vendor's name or the ERP company; ``backend/docs/vendor-management.md``
   § Duplicate ERP links has the query that lists them in full.
2. Builds ``uq_vendors_erp_link`` on ``(organization_id, erp_link_realm,
   erp_vendor_id) NULLS NOT DISTINCT WHERE erp_vendor_id IS NOT NULL``, the
   index ``models.vendor.Vendor`` declares, so ``create_all`` builds the same
   on a fresh tenant.

``NULLS NOT DISTINCT`` (PostgreSQL 15+; every database here is 16) because a
NULL realm is a company to the sync too: a no-realm pull looks a record up as
``erp_link_realm IS NULL``, so two NULL-realm holders of one id break it the
same way. With plain NULL semantics those rows, the pre-0115 links, would be
the one place the duplicate survives. A NULL-realm link and a named one may
share an id: they may be two companies, and a sync under the named one
re-judges the NULL link by evidence (§284).

Placement
---------
TENANT DB ONLY: gated on ``vendors`` and ``vendor_erp_links`` (0125, tenant
only) existing, so a no-op on the control plane; fanned out by
``scripts/migrate_all_tenants.py``, whose non-zero exit on a refusal stops the
deploy before new code serves an unmigrated tenant.

Idempotent: the check finds nothing once the index exists, and the index is
``IF NOT EXISTS``. The plain ``CREATE UNIQUE INDEX`` runs inside the migration
transaction and blocks writes to ``vendors`` while it builds — instant at
today's sizes. Downgrade drops the index.

Revision ID: 0140_vendor_erp_link_unique
Revises: 0139_sanctions_false_positive
Create Date: 2026-11-21

27 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.
"""

from sqlalchemy import text

from alembic import op

revision = "0140_vendor_erp_link_unique"
down_revision = "0139_sanctions_false_positive"
branch_labels = None
depends_on = None

INDEX = "uq_vendors_erp_link"

#: Spelled out, not interpolated, so ``tests/test_migration_model_index_parity``
#: scans it and holds the model to declaring it.
CREATE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_vendors_erp_link "
    "ON vendors (organization_id, erp_link_realm, erp_vendor_id) NULLS NOT DISTINCT "
    "WHERE erp_vendor_id IS NOT NULL"
)

#: Every group of vendors holding one id in one company. ``GROUP BY`` puts NULL
#: realms together, exactly as ``NULLS NOT DISTINCT`` will.
DUPLICATES = (
    "SELECT array_agg(id ORDER BY id) AS vendor_ids FROM vendors "
    "WHERE erp_vendor_id IS NOT NULL "
    "GROUP BY organization_id, erp_link_realm, erp_vendor_id HAVING count(*) > 1 "
    "ORDER BY min(id::text)"
)

#: How many vendor ids the refusal names; the doc's query lists the rest.
NAMED_IDS = 10


class DuplicateErpVendorLink(RuntimeError):
    """Two or more vendors hold one ERP record in one company; an operator must
    choose which keeps it."""


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


def _refuse_duplicates() -> None:
    bind = op.get_bind()
    groups = [list(row.vendor_ids) for row in bind.execute(text(DUPLICATES))]
    if not groups:
        return
    rows = sum(len(g) for g in groups)
    ids = [str(v) for g in groups for v in g]
    named = ", ".join(ids[:NAMED_IDS])
    if rows > NAMED_IDS:
        named += f" and {rows - NAMED_IDS} more"
    database = bind.execute(text("SELECT current_database()")).scalar()
    raise DuplicateErpVendorLink(
        f"0140: the vendors table in database {database} holds {len(groups)} ERP vendor link(s) "
        f"shared by {rows} vendors in the same ERP company (vendor ids: {named}). For each "
        "group, merge the vendors if they are one supplier, or unlink the stale record from "
        "the others, then re-run the migration. List every group with the query in "
        "backend/docs/vendor-management.md § Duplicate ERP links. Nothing was changed."
    )


def upgrade() -> None:
    if not _is_tenant():
        return
    _refuse_duplicates()
    op.execute(CREATE_INDEX)


def downgrade() -> None:
    if not _is_tenant():
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
