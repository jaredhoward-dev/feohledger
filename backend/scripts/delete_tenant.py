"""Delete a tenant completely, from the command line.

Thin wrapper around ``app.services.tenant_deletion.delete_tenant`` so the CLI
and ``deploy/remove-tenant.sh`` share one code path — the same arrangement
``create_tenant.py`` has with provisioning.

This is the most destructive operation in the product: it drops a customer's
database, removes their documents from object storage, and deletes their
organisation and users. It exists because ``/legal/dpa`` § 13 and clause 10 of
the Terms promise exactly that within 60 days of termination, and a promise
with no mechanism behind it is a promise we would have to break.

Nothing here is undoable from the application. The confirmation is therefore
deliberately awkward — the operator types the slug back — and ``--dry-run``
prints the same inventory without touching anything.

It does NOT delete the tenant's nightly backup dumps: nothing the VM runs may
(``docs/decisions.md`` §490). Instead every deletion records a
``workspace_closures`` row before it starts — the same queue a self-service
closure writes — and the approval-gated ``backup-erasure`` workflow erases the
dumps and marks that row. The row also holds the slug
(``utils/slug.ensure_slug_not_held``) until then, because a new workspace under
it would share the dumps' name. ``--no-backup-store`` is for a deployment with
no backup bucket at all: the row is recorded with its backups already cleared.

Usage:
    python scripts/delete_tenant.py --slug acme --dry-run
    python scripts/delete_tenant.py --slug acme --confirm acme
    python scripts/delete_tenant.py --slug acme --yes      # for a wrapper script
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.tenant_deletion import (
    TenantDeletionError,
    TenantNotFound,
    delete_tenant,
    format_confirmation,
    plan_tenant_deletion,
)
from app.services.workspace_closure import (
    latest_closure_for_slug,
    record_operator_removal_completed,
    record_operator_removal_started,
)

#: No organisation has the slug, and a removal of it is on record: the live
#: data is already gone. Distinct from 2 (any other refusal) and from an
#: exception's 1, because `deploy/remove-tenant.sh` treats exactly this one as
#: "already removed" — every other failure has to stop it.
EXIT_NOT_FOUND = 4
#: No organisation has the slug and no removal of it was ever recorded: most
#: likely a typo. `deploy/remove-tenant.sh` refuses on it.
EXIT_NO_RECORD = 5

#: `backups_cleared_by` for a removal on a deployment with no backup store.
NO_BACKUP_STORE = "no-backup-store"


async def _report_missing(slug: str) -> int:
    """Say what became of a slug with no organisation: whether its removal is
    on record, and whether its backups are erased yet."""
    record = await latest_closure_for_slug(slug)
    if record is None:
        print(f"delete_tenant: no organisation with slug {slug!r}, and no removal of it on record.")
        return EXIT_NO_RECORD
    when = (record.completed_at or record.started_at).date().isoformat()
    if record.backups_cleared_at is not None:
        backups = f"erased {record.backups_cleared_at.date().isoformat()}"
    else:
        backups = "still owed: erased by the approval-gated backup-erasure workflow"
    print(
        f"No organisation with slug {slug!r}: removed {when} "
        f"(organisation {record.organization_id})."
    )
    print(f"Backups:           {backups}")
    return EXIT_NOT_FOUND


def _print_plan(plan) -> None:
    print(f"Tenant:            {plan.name} ({plan.slug})")
    print(f"Organisation id:   {plan.organization_id}")
    print(
        f"Tenant database:   {plan.db_name} "
        + ("(exists — will be dropped)" if plan.database_exists else "(already absent)")
    )
    print(f"Documents:         everything under s3://<bucket>/{plan.organization_id}/")
    print(f"Control-plane rows ({plan.total_control_rows} total):")
    for table, count in plan.control_rows.items():
        print(f"    {table:<24} {count}")


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete a tenant: database, documents, and control-plane records."
    )
    parser.add_argument("--slug", required=True, help="Tenant slug, e.g. 'acme'")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted and exit without changing anything.",
    )
    parser.add_argument(
        "--confirm",
        default=None,
        help="Type the slug again to confirm. Required unless --yes or --dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the typed confirmation. For a wrapper that already confirmed "
        "(deploy/remove-tenant.sh does).",
    )
    parser.add_argument(
        "--no-backup-store",
        action="store_true",
        help="This deployment keeps no backup dumps (no BACKUP_S3_BUCKET): record the "
        "removal with nothing owed to the backup-erasure workflow.",
    )
    args = parser.parse_args()

    try:
        plan = await plan_tenant_deletion(args.slug)
    except TenantNotFound:
        return await _report_missing(args.slug)
    except TenantDeletionError as exc:
        print(f"delete_tenant: {exc}", file=sys.stderr)
        return 2

    _print_plan(plan)

    if args.dry_run:
        print("\n--dry-run: nothing was deleted.")
        return 0

    if not args.yes and args.confirm != args.slug:
        # Not a formality. Everything above is about to be destroyed with no
        # application-level path back, so the operator has to name the tenant a
        # second time rather than answer a prompt they have stopped reading.
        print(
            f"\nRefusing to delete without confirmation. Re-run with --confirm {args.slug}",
            file=sys.stderr,
        )
        return 3

    # Recorded BEFORE anything is deleted, so a run that dies part-way is on
    # the queue (`workspace_closures.py list`) and holds the slug.
    await record_operator_removal_started(plan.organization_id, plan.slug)

    try:
        result = await delete_tenant(args.slug)
    except TenantDeletionError as exc:
        print(f"delete_tenant: {exc}", file=sys.stderr)
        return 2

    await record_operator_removal_completed(
        plan.organization_id,
        result,
        backups_cleared_by=NO_BACKUP_STORE if args.no_backup_store else None,
    )

    print()
    print(format_confirmation(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
