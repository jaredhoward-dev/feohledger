#!/usr/bin/env bash
# Remove a tenant from the minimal single-VM stack: destroy its documents,
# database and control-plane records, and put its backup dumps on the queue
# that the approval-gated backup-erasure workflow clears.
#
# This is the deletion `/legal/dpa` § 13 and clause 10 of the Terms promise a
# customer within 60 days of termination. It is the inverse of add-tenant.sh and
# the most destructive script in the repo — nothing it does can be undone from
# the application.
#
# It does NOT delete backups, and cannot (docs/decisions.md §490). Nothing the
# VM can trigger on its own may delete a backup: the VM role (infra/compute.tf,
# Sid `Backups`) has no delete on the backup bucket, so a compromised VM cannot
# wipe the backup history. Instead scripts/delete_tenant.py records the removal
# in `workspace_closures` before it deletes anything — the same queue a
# self-service closure writes — and .github/workflows/backup-erasure.yml, in a
# job a human approves and under a role only that job can assume, deletes every
# version and delete marker of pg/*/feoh_<slug>.dump, verifies none is left and
# marks the row cleared. The row also holds the slug until then
# (utils/slug.ensure_slug_not_held): a new workspace under it would share the
# dumps' name. Its daily check fails the workflow red if an owed erasure passes
# 45 days, inside the DPA's 60.
#
# So the confirmation this prints covers the live data only. Send the customer
# the backup confirmation once `scripts/workspace_closures.py list` no longer
# shows the organisation (docs/founder-runbooks/workspace-closure.md).
#
# There is no web step. Since docs/decisions.md §364 the VM serves only the
# API: <slug>.<APP_DOMAIN> is CloudFront's wildcard, which serves the same
# static SPA to every host, and with the organisation gone the API answers that
# slug as an unknown tenant. A customer vanity domain is the exception: its
# block in tenants.caddy is removed by hand (custom-domain-provisioning.md
# § Rollback), and this script says so at the end.
#
# --no-backup-store is for a deployment that keeps no backups at all: the
# removal is recorded with nothing owed, which releases the slug after its
# cooldown. It is never inferred — an empty BACKUP_S3_BUCKET without it is an
# error, since a misread env would otherwise quietly drop an owed erasure.
#
# Usage: remove-tenant.sh <slug> [--dry-run] [--yes] [--no-backup-store]
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "remove-tenant.sh: $*" >&2
	exit 1
}
usage() {
	die "usage: remove-tenant.sh <slug> [--dry-run] [--yes] [--no-backup-store]"
}

SLUG="${1:-}"
[ -n "$SLUG" ] || usage
shift
DRY_RUN=0 ASSUME_YES=0 NO_BACKUP_STORE=0
while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run) DRY_RUN=1 && shift ;;
	--yes) ASSUME_YES=1 && shift ;;
	--no-backup-store) NO_BACKUP_STORE=1 && shift ;;
	*) usage ;;
	esac
done
echo "$SLUG" | grep -Eq '^[a-z0-9](-?[a-z0-9])*$' || die "invalid slug '$SLUG'"
[ -f .env ] || die "deploy/.env missing — run deploy.sh at least once first."
# Same compose invocation as every other deploy script (deploy/lib.sh) — the
# api it execs into is the same container in either database mode.
feoh_load_db_mode || die "could not determine the database mode from deploy/.env."

APP_DOMAIN=$(feoh_env APP_DOMAIN)
[ -n "$APP_DOMAIN" ] || die "APP_DOMAIN not set in the sops env."
HOST="${SLUG}.${APP_DOMAIN}"
DB_NAME="feoh_${SLUG}"

BUCKET="${BACKUP_S3_BUCKET:-}"
[ -n "$BUCKET" ] || BUCKET=$(feoh_env BACKUP_S3_BUCKET)
# With no backup store there is nothing for the erasure workflow to do, and
# the removal is recorded with its backups already cleared — but only when the
# operator says so. The two must agree.
BACKUP_FLAGS=()
if [ "$NO_BACKUP_STORE" -eq 1 ]; then
	[ -z "$BUCKET" ] || die "--no-backup-store, but BACKUP_S3_BUCKET is set: this deployment keeps backups. Drop the flag."
	BACKUP_FLAGS=(--no-backup-store)
elif [ -z "$BUCKET" ]; then
	die "BACKUP_S3_BUCKET is not set. If this deployment really keeps no backups, re-run with --no-backup-store; otherwise fix deploy/.env."
fi

# --- 0. Show what is about to go, from the application's own inventory -------

# Only delete_tenant.py's exit 4 means "already removed": no organisation has
# the slug and its removal is on record. It prints that record, including
# whether the backups are erased yet, and there is nothing left for this script
# to do. Exit 5 is no organisation AND no record — most likely a typo.
#
# Any other failure — the api container down, the database unreachable, a
# partner org refused — stops the run before it changes anything. (Before
# §490 any failure here was once read as "already deleted", and the run went on
# to delete the backups of a tenant whose live data was still there.)
echo "==> inventory"
INVENTORY_RC=0
"${COMPOSE[@]}" exec -T api python scripts/delete_tenant.py --slug "$SLUG" --dry-run || INVENTORY_RC=$?
case "$INVENTORY_RC" in
0) ;;
4)
	echo
	echo "Tenant ${SLUG} was already removed; nothing to do here."
	echo "Its backups are the backup-erasure workflow's (see 'Backups' above)."
	exit 0
	;;
5) die "nothing found for slug '$SLUG' — no organisation and no removal on record. Check the spelling." ;;
*) die "the tenant inventory failed (delete_tenant.py exit ${INVENTORY_RC}, error above) — nothing was changed." ;;
esac

if [ -n "$BUCKET" ]; then
	echo "Backup objects:    pg/*/${DB_NAME}.dump — queued for the approval-gated backup-erasure"
	echo "                   workflow (not deleted from this VM, docs/decisions.md §490)"
else
	echo "Backup objects:    BACKUP_S3_BUCKET is not set — no backup store; nothing will be owed."
fi

if [ "$DRY_RUN" -eq 1 ]; then
	echo
	echo "--dry-run: nothing was changed."
	exit 0
fi

# --- 1. Confirm -------------------------------------------------------------

if [ "$ASSUME_YES" -ne 1 ]; then
	echo
	echo "This destroys the tenant's documents, database and control-plane records,"
	echo "and queues its backups for erasure. It cannot be undone."
	printf "Type the slug (%s) to confirm: " "$SLUG"
	read -r TYPED
	[ "$TYPED" = "$SLUG" ] || die "confirmation did not match — nothing was changed."
fi

# --- 2. Documents, database, control plane, and the owed-backups record ------

echo "==> deleting tenant data"
"${COMPOSE[@]}" exec -T api python scripts/delete_tenant.py --slug "$SLUG" --yes "${BACKUP_FLAGS[@]}"

if [ -n "$BUCKET" ]; then
	BACKUP_RESULT="owed — the approval-gated backup-erasure workflow erases them (not confirmed here)"
else
	BACKUP_RESULT="no backup store configured"
fi

# --- 3. The written confirmation, and what it does not cover ----------------

cat <<-EOF

	Tenant ${SLUG} removed.

	  tenant host:      ${HOST} (no longer resolves to a tenant)
	  tenant database:  ${DB_NAME}
	  backups:          ${BACKUP_RESULT}

	These residues remain, as /legal/dpa § 13 discloses:

	  - The nightly CONTROL-PLANE dump (pg/*/feohledger.dump) is shared across
	    every tenant and is not selectively editable, so this tenant's employee
	    rows persist inside it until it ages out on the ordinary backup
	    retention cycle (90 days by default).
	  - Any audit event already shipped to write-once archival cannot be deleted
	    before its retention period expires.
EOF
# An external database (RDS) adds the residue the DPA's "volume-level snapshot"
# sentence covers: its automated backups are whole-instance storage snapshots
# plus transaction logs, not selectively editable, kept for the instance's
# backup retention period (the point-in-time restore window).
if [ "$DB_MODE" = external ]; then
	cat <<-EOF
		  - The database server's automated backups (RDS snapshots + the
		    point-in-time-restore window) hold the whole instance, this tenant's
		    database included, and are not selectively editable; they expire on
		    the instance's backup retention period.
	EOF
fi
cat <<-EOF

	Send the customer the confirmation printed above under "Tenant deletion",
	and the backup confirmation once the backup-erasure workflow has cleared it.

	If this tenant had a vanity domain, remove its block from deploy/tenants.caddy
	and reload Caddy (docs/founder-runbooks/custom-domain-provisioning.md
	§ Rollback / de-provisioning). Its registration went with the organisation.
EOF
