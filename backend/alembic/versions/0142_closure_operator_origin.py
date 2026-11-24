"""``workspace_closures.requested_by_user_id`` may be NULL: an operator removal.

Why
---
``docs/decisions.md`` §490. Backup erasure moved off the VM into an
approval-gated GitHub Actions job, and that job reads ONE queue: the
``workspace_closures`` rows still owed a backup deletion. A self-service closure
already writes one. The operator path (``scripts/delete_tenant.py``, behind
``deploy/remove-tenant.sh``) now writes one too, so its backups are erased by
the same job and its slug is held by the same rule
(``utils/slug.ensure_slug_not_held``) — without the hold, a new workspace under
the slug would share the removed one's ``feoh_<slug>.dump`` name, and erasing
the old dumps would erase the new tenant's.

An operator removal has no requesting user: nobody signed in to the API asked
for it. Recording an invented user id would be dishonest, so the column becomes
nullable. Such a row is told apart by ``step_up_proof = 'operator'`` (the proof
is shell access to the VM), and its ``requested_by_roles`` is NULL.

CONTROL PLANE ONLY: gated on ``workspace_closures`` existing (0119 creates it
on the control plane alone), so a no-op on every tenant DB. A fresh control
plane gets the column from ``create_all``; the model declares it nullable.

Idempotent: ``DROP NOT NULL`` on a nullable column is a no-op.

Revision ID: 0142_closure_operator_origin
Revises: 0141_vendor_erp_link_unique
Create Date: 2026-11-24

28 characters (``alembic_version.version_num`` is ``VARCHAR(32)``).
"""

from sqlalchemy import text

from alembic import op

revision = "0142_closure_operator_origin"
down_revision = "0141_vendor_erp_link_unique"
branch_labels = None
depends_on = None


def _has_closures() -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'workspace_closures'"
            )
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not _has_closures():
        return
    op.execute("ALTER TABLE workspace_closures ALTER COLUMN requested_by_user_id DROP NOT NULL")


def downgrade() -> None:
    """No rollback, deliberately.

    Restoring NOT NULL would fail on every operator removal recorded since, and
    deleting those rows to make it pass would drop owed backup erasures from the
    queue and release slugs the hold exists to keep. The older code never writes
    a NULL, so leaving the column nullable costs nothing.
    """
