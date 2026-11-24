"""Closed or removed workspaces, and the backup erasure still owed on them.

A self-service closure (`POST /api/organization/close`, `docs/decisions.md`
§306) and an operator removal (`scripts/delete_tenant.py`, behind
`deploy/remove-tenant.sh`) delete the tenant's database, documents and
control-plane rows, but not its nightly backup dumps: those are in the backup
bucket, which neither the API nor the VM may delete from. `/legal/dpa` § 13
promises they go within 60 days. Each removal leaves a `workspace_closures` row,
and the approval-gated `backup-erasure` GitHub Actions workflow (§490) erases
the dumps and marks the row, through the SSM documents in
`infra/backup_erasure.tf`, which run this script on the VM:

    python scripts/workspace_closures.py owed --summary   # counts only (the daily check)
    python scripts/workspace_closures.py owed --json      # what to erase (the approved job)
    python scripts/workspace_closures.py mark-cleared <org-id> --operator github-actions-run-<id>

`owed` prints ONE line, `FEOH_ERASURE <json>`, so a caller reading SSM output
finds it among anything else the container writes. `--summary` names no
workspace: its counts are what the public workflow log shows.

For a person: `list` shows the same queue, and closures that began and never
finished while their workspace still exists — finish those with
`deploy/remove-tenant.sh <slug>`.

Runbook: `docs/founder-runbooks/workspace-closure.md`.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.workspace_closure import (
    ERASURE_BATCH,
    ERASURE_COOL_OFF,
    ERASURE_OVERDUE_AFTER,
    closures_owing_backups,
    mark_backups_cleared,
    owed_backup_erasures,
    unfinished_closures,
)

#: `mark-cleared` found no finished closure of that organisation still owing backups.
EXIT_NOTHING_MARKED = 4

#: The prefix of `owed`'s one machine-readable line (`deploy/backup-erasure.sh`
#: looks for it).
MARKER = "FEOH_ERASURE"


def _when(value) -> str:
    return value.date().isoformat() if value else "?"


async def _list() -> int:
    owed = await closures_owing_backups()
    unfinished = await unfinished_closures()
    if not owed and not unfinished:
        print("No closed workspace is owed a backup deletion, and none is unfinished.")
        return 0
    if owed:
        print(f"{len(owed)} closed workspace(s) still owed a backup deletion (oldest first):")
        for row in owed:
            state = "" if row.status == "completed" else "   (closure did not finish)"
            print(
                f"  {row.slug:<32} closed {_when(row.completed_at)}   "
                f"org {row.organization_id}{state}"
            )
    if unfinished:
        print(
            f"{len(unfinished)} closure(s) began and did not finish; the workspace still "
            "exists (finish with deploy/remove-tenant.sh <slug>):"
        )
        for row in unfinished:
            failure = f"   last failure {row.last_failure}" if row.last_failure else ""
            print(
                f"  {row.slug:<32} started {_when(row.started_at)}   "
                f"org {row.organization_id}{failure}"
            )
    return 0


async def _owed(*, detail: bool) -> int:
    owed = await owed_backup_erasures()
    if detail:
        # Identifiers: read only by the approved erase job, which masks every
        # one before it uses any. At most ERASURE_BATCH of them (SSM's output
        # cap): the erasable ones first, oldest first, so blocked entries can
        # never starve the queue; blocked ones fill any room left. `total` and
        # `blocked_total` count the whole queue, listed or not.
        batch = [e for e in owed if e.blocked is None] + [e for e in owed if e.blocked]
        payload: dict = {
            "total": len(owed),
            "blocked_total": sum(1 for e in owed if e.blocked is not None),
            "owed": [
                {
                    "organization_id": str(e.organization_id),
                    "slug": e.slug,
                    "database": e.database,
                    "closed_on": e.closed_at.date().isoformat(),
                    "age_days": e.age_days,
                    "blocked": e.blocked,
                }
                for e in batch[:ERASURE_BATCH]
            ],
        }
    else:
        # Counts only: the daily check prints these to a PUBLIC workflow log.
        payload = {
            "owed": len(owed),
            "ready": sum(1 for e in owed if e.blocked is None and not e.cooling),
            "cooling": sum(1 for e in owed if e.blocked is None and e.cooling),
            "cool_off_days": ERASURE_COOL_OFF.days,
            "blocked": sum(1 for e in owed if e.blocked is not None),
            "overdue": sum(1 for e in owed if e.overdue),
            "oldest_age_days": max((e.age_days for e in owed), default=0),
            "overdue_after_days": ERASURE_OVERDUE_AFTER.days,
        }
    print(f"{MARKER} {json.dumps(payload, sort_keys=True)}")
    return 0


async def _mark(organization_id: uuid.UUID, operator: str) -> int:
    marked = await mark_backups_cleared(organization_id, operator=operator)
    if not marked:
        print(
            f"No finished closure of organisation {organization_id} is owed a backup "
            "deletion; nothing marked."
        )
        return EXIT_NOTHING_MARKED
    print(
        f"Marked organisation {organization_id}: backups cleared by {operator}. Its slug "
        "becomes available once the closure cooldown has passed."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="closures owed a backup deletion, and unfinished ones")
    owed = sub.add_parser("owed", help="the backup-erasure queue, machine-readable")
    form = owed.add_mutually_exclusive_group(required=True)
    form.add_argument("--summary", action="store_true", help="counts only, no identifiers")
    form.add_argument("--json", action="store_true", help="every owed erasure, with its slug")
    mark = sub.add_parser("mark-cleared", help="record that a closure's backups are removed")
    # The organisation id, not the slug: `list` prints it, and it is the one
    # identifier a later workspace under the same name can never share.
    mark.add_argument("organization_id", type=uuid.UUID)
    # Who removed them, recorded on the row: `github-actions-run-<id>` from the
    # erasure workflow, or a handle or ticket reference for a hand-run clear.
    mark.add_argument("--operator", required=True)
    args = parser.parse_args(argv)
    if args.command == "list":
        return asyncio.run(_list())
    if args.command == "owed":
        return asyncio.run(_owed(detail=args.json))
    operator = args.operator.strip()
    if not operator:
        parser.error("--operator must not be blank")
    return asyncio.run(_mark(args.organization_id, operator))


if __name__ == "__main__":
    sys.exit(main())
