#!/usr/bin/env bash
# The VM's half of backup erasure (docs/decisions.md §490): read the queue of
# removed workspaces still owed a backup deletion, and mark one cleared.
#
# It deletes nothing, and cannot: the VM role may not delete from the backup
# bucket. The deleting is done by the approval-gated `erase` job of
# .github/workflows/backup-erasure.yml, under its own role, from a GitHub
# runner. That job reaches this script only through the SSM command documents
# in infra/backup_erasure.tf, which run it as ec2-user with fixed arguments:
#
#   summary                       counts only — the daily `check` job
#   owed                          every owed erasure, with its slug — `erase` job
#   mark-cleared <org-uuid> <run> after the job verified the dumps are gone
#
# SSM refuses a parameter that is not a UUID / a run number before a shell
# exists; this script checks the same shapes again, so a hand run with a typo
# stops here rather than in Python.
#
# Usage: backup-erasure-queue.sh summary | owed | mark-cleared <org-uuid> <run-id>
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "backup-erasure-queue.sh: $*" >&2
	exit 1
}
usage() {
	die "usage: backup-erasure-queue.sh summary | owed | mark-cleared <org-uuid> <run-id>"
}

[ -f .env ] || die "deploy/.env missing — run deploy.sh at least once first."
feoh_load_db_mode || die "could not determine the database mode from deploy/.env."

UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

case "${1:-}" in
summary)
	[ $# -eq 1 ] || usage
	exec "${COMPOSE[@]}" exec -T api python scripts/workspace_closures.py owed --summary
	;;
owed)
	[ $# -eq 1 ] || usage
	exec "${COMPOSE[@]}" exec -T api python scripts/workspace_closures.py owed --json
	;;
mark-cleared)
	[ $# -eq 3 ] || usage
	echo "$2" | grep -Eq "$UUID_RE" || die "not an organisation id (a lowercase UUID)."
	echo "$3" | grep -Eq '^[0-9]{1,20}$' || die "not a GitHub run id."
	exec "${COMPOSE[@]}" exec -T api python scripts/workspace_closures.py \
		mark-cleared "$2" --operator "github-actions-run-$3"
	;;
*) usage ;;
esac
