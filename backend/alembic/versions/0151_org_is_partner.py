"""Operator-granted partner capability: ``organizations.is_partner`` (control plane).

Why
---
``docs/decisions.md`` §574. "Partner" used to be derived: any org referenced by
at least one child (``parent_org_id``, migration 0065). Nothing said which
workspaces may act as a partner, so the Partner Admin page — provisioning,
attaching and branding child tenants — was offered to every admin of every
workspace, and the first child it created is what made the org a partner.
Acting as a reseller is a commercial arrangement the operator grants, not a
plan tier a workspace can buy itself into and not something an admin can
switch on, so it is a column the operator sets
(``scripts/set_partner_capability.py``), read by ``/api/auth/me`` and required
by every ``/api/partner`` route.

Backfill
--------
An org that already parents a child IS acting as a partner — that is a fact
about the hierarchy, not a guess — so every org referenced by a
``parent_org_id`` gets ``is_partner = true``. Every other org starts false.
Nothing else is inferred (plan, name, settings): a proxy would either grant a
reseller's powers to a workspace nobody agreed to, or take them from one that
uses them.

Revision ID: 0151_org_is_partner
Revises: 0150_pay_schedule_current_idx
Create Date: 2026-11-25

19 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

CONTROL PLANE ONLY: gated on ``organizations`` existing (orgs never live in a
tenant DB), so it no-ops on every tenant. Mirrors
``app.models.organization.Organization.is_partner`` (``Boolean``, NOT NULL,
server default ``false``) so a ``create_all`` control plane matches a migrated
one. Idempotent: ``ADD COLUMN IF NOT EXISTS``, and the backfill only sets
``true`` where a child exists, so a re-run changes nothing. The downgrade drops
the column (the grant is lost; the hierarchy it was backfilled from is not).
"""

from sqlalchemy import text

from alembic import op

revision = "0151_org_is_partner"
down_revision = "0150_pay_schedule_current_idx"
branch_labels = None
depends_on = None


def _is_control_db() -> bool:
    """The control DB is the one with the ``organizations`` table."""
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
        "ALTER TABLE organizations "
        "ADD COLUMN IF NOT EXISTS is_partner boolean NOT NULL DEFAULT false"
    )
    # An org that already parents a child acts as a partner: keep its access.
    op.execute(
        "UPDATE organizations SET is_partner = true "
        "WHERE is_partner = false AND id IN ("
        "  SELECT parent_org_id FROM organizations WHERE parent_org_id IS NOT NULL"
        ")"
    )


def downgrade() -> None:
    if not _is_control_db():
        return
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS is_partner")
