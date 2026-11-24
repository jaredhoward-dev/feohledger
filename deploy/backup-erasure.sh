#!/usr/bin/env bash
# Erase the nightly backup dumps of removed workspaces — run by
# .github/workflows/backup-erasure.yml on a GitHub runner, never on the VM
# (docs/decisions.md §490).
#
# Why here and not in remove-tenant.sh: nothing the VM can trigger on its own
# may delete a backup. A compromised VM that could erase backups could erase
# all of them, so the VM role cannot, and deletion runs only in a job a human
# approves, under a role only that job can assume (infra/backup_erasure.tf).
#
#   check   counts the queue through the read-only `summary` SSM document and
#           writes ready / blocked / overdue to $GITHUB_OUTPUT. Prints counts
#           only — this log is PUBLIC (public repo).
#   erase   reads the queue through the `owed` document, masks every slug,
#           organisation id and database name BEFORE using any, then per
#           workspace: lists every version and delete marker of
#           pg/*/feoh_<slug>.dump, deletes each by version id, re-lists and
#           requires none left, and only then marks the closure cleared through
#           the `mark-cleared` document. A workspace that fails anywhere is not
#           marked; the rest carry on and the run fails at the end.
#
# The VM chooses what is owed, and a compromised VM could list a live tenant.
# So before deleting anything the job checks the bucket itself, which the VM
# cannot rewrite for past nights (it has no delete there):
#   (a) the newest nightly prefix pg/<YYYY-MM-DD>/ (backup.sh, UTC date) is at
#       most MAX_BACKUP_AGE_DAYS old — backups are running, so (b) means
#       something. Otherwise nothing is erased and the run fails.
#   (b) backup.sh dumps EVERY feoh_* database every night. A dump of an owed
#       database dated AFTER its closure means it was still there to dump: a
#       forged entry or a removal that failed. Refused, and the run fails.
#       A dump in any of the newest RECENT_NIGHTS prefixes (dated on or before
#       the closure) is deferred: a restore window for a mistaken removal.
#   (c) a closure younger than COOL_OFF_DAYS is deferred (a cool-off; the
#       same number as workspace_closure.ERASURE_COOL_OFF).
# Deferred is not a failure: the entry stays owed and a later run erases it,
# well inside the 45-day alarm.
#
# Environment:
#   INSTANCE_ID       the app VM (a secret: it is not public)
#   BACKUP_BUCKET     the backup bucket (erase; a secret: it embeds the account id)
#   GITHUB_RUN_ID     recorded on each cleared row as github-actions-run-<id> (erase)
#   SUMMARY_DOCUMENT, OWED_DOCUMENT, MARK_DOCUMENT
#                     the SSM documents, defaulting to infra/backup_erasure.tf's names
#   SSM_POLL_SECONDS  poll interval (default 5); SSM_WAIT_SECONDS gives up after (default 600)
#
# Usage: backup-erasure.sh check | erase
set -euo pipefail

die() {
	echo "::error::backup-erasure.sh: $*" >&2
	exit 1
}

SUMMARY_DOCUMENT=${SUMMARY_DOCUMENT:-feohledger-backup-erasure-summary}
OWED_DOCUMENT=${OWED_DOCUMENT:-feohledger-backup-erasure-owed}
MARK_DOCUMENT=${MARK_DOCUMENT:-feohledger-backup-erasure-mark-cleared}
SSM_POLL_SECONDS=${SSM_POLL_SECONDS:-5}
SSM_WAIT_SECONDS=${SSM_WAIT_SECONDS:-600}
MAX_BACKUP_AGE_DAYS=2
RECENT_NIGHTS=3
COOL_OFF_DAYS=7
GITHUB_OUTPUT=${GITHUB_OUTPUT:-/dev/null}
GITHUB_STEP_SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/null}

UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
# feoh_ + a slug (utils/slug.py: lowercase letters, digits, single hyphens).
# Never `feohledger` — the shared control-plane dump — which has no underscore,
# and the erase role is denied it in IAM as well.
DATABASE_RE='^feoh_[a-z0-9]+(-[a-z0-9]+)*$'
DATE_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'

[ -n "${INSTANCE_ID:-}" ] || die "INSTANCE_ID is not set (the VM_INSTANCE_ID secret)."

# ssm_run <document> [<parameters>] runs one SSM command document on the VM,
# waits for it, and leaves its stdout in SSM_STDOUT. Returns non-zero unless it
# ended Success. Neither its output nor its error is printed: the `owed`
# document's output names workspaces.
SSM_STDOUT=""
ssm_run() {
	local doc=$1 params=${2:-} cid status deadline
	local args=(--document-name "$doc" --instance-ids "$INSTANCE_ID" --timeout-seconds 300
		--comment "backup-erasure run ${GITHUB_RUN_ID:-manual}" --query Command.CommandId --output text)
	[ -z "$params" ] || args+=(--parameters "$params")
	SSM_STDOUT=""
	cid=$(aws ssm send-command "${args[@]}") || return 1
	deadline=$(($(date +%s) + SSM_WAIT_SECONDS))
	while :; do
		# Right after SendCommand the invocation may not exist yet
		# (InvocationDoesNotExist): that is "pending".
		if ! status=$(aws ssm get-command-invocation --command-id "$cid" \
			--instance-id "$INSTANCE_ID" --query Status --output text 2>/dev/null); then
			status=Pending
		fi
		case "$status" in
		Pending | InProgress | Delayed | Cancelling) ;;
		*) break ;;
		esac
		if [ "$(date +%s)" -ge "$deadline" ]; then
			echo "::error::SSM command on ${doc} still ${status} after ${SSM_WAIT_SECONDS}s." >&2
			return 1
		fi
		sleep "$SSM_POLL_SECONDS"
	done
	SSM_STDOUT=$(aws ssm get-command-invocation --command-id "$cid" \
		--instance-id "$INSTANCE_ID" --query StandardOutputContent --output text) || return 1
	if [ "$status" != Success ]; then
		echo "::error::SSM command on ${doc} ended ${status} (its output is in the SSM console, not here: it may name a workspace)." >&2
		return 1
	fi
}

# The one `FEOH_ERASURE <json>` line scripts/workspace_closures.py prints.
queue_json() {
	printf '%s\n' "$SSM_STDOUT" | sed -n 's/^FEOH_ERASURE //p' | tail -1
}

is_count() {
	[[ "$1" =~ ^[0-9]+$ ]]
}

check() {
	ssm_run "$SUMMARY_DOCUMENT" || die "could not read the erasure queue summary from the VM."
	local json owed ready blocked overdue oldest after
	json=$(queue_json)
	[ -n "$json" ] || die "the queue summary printed no FEOH_ERASURE line."
	local cooling
	owed=$(jq -r '.owed' <<<"$json")
	ready=$(jq -r '.ready' <<<"$json")
	cooling=$(jq -r '.cooling' <<<"$json")
	blocked=$(jq -r '.blocked' <<<"$json")
	overdue=$(jq -r '.overdue' <<<"$json")
	oldest=$(jq -r '.oldest_age_days' <<<"$json")
	after=$(jq -r '.overdue_after_days' <<<"$json")
	for n in "$owed" "$ready" "$cooling" "$blocked" "$overdue" "$oldest" "$after"; do
		is_count "$n" || die "the queue summary is not the expected shape."
	done
	echo "Owed backup erasures: ${owed} (ready ${ready}, cooling off ${cooling}, blocked ${blocked}, overdue ${overdue}; oldest ${oldest} day(s); overdue after ${after})."
	{
		echo "ready=${ready}"
		echo "blocked=${blocked}"
		echo "overdue=${overdue}"
	} >>"$GITHUB_OUTPUT"
	{
		echo "### Backup erasure queue"
		echo
		echo "Owed: ${owed} · ready: ${ready} · cooling off: ${cooling} · blocked: ${blocked} · overdue (≥ ${after} days): ${overdue} · oldest: ${oldest} day(s)"
	} >>"$GITHUB_STEP_SUMMARY"
}

# Every version and every delete marker of pg/<date>/<database>.dump, one
# "<key>\t<version id>" per line. Same listing as remove-tenant.sh used before
# §490, for the same two reasons:
#   - each list is filtered SEPARATELY and flattened after. The obvious
#     `[Versions, DeleteMarkers][][?ends_with(...)]` runs fine and matches
#     NOTHING (the filter binds to the projection, not its elements), so it
#     would erase nothing and report success.
#   - the `/` before the name is load-bearing: without it `feoh_acme.dump` would
#     also match a tenant named `not-acme`.
# The prefix is exactly `pg/`: the erase role may list versions under no other.
list_versions() {
	local database=$1 listing
	listing=$(aws s3api list-object-versions --bucket "$BACKUP_BUCKET" --prefix "pg/" \
		--query "[Versions[?ends_with(Key, '/${database}.dump')], DeleteMarkers[?ends_with(Key, '/${database}.dump')]][][].[Key,VersionId]" \
		--output text) || return 1
	printf '%s\n' "$listing" | grep -v '^None' | grep . || true
}

# The nightly dates in the bucket, oldest first: pg/<YYYY-MM-DD>/ prefixes.
# Listed through ListObjectVersions (prefix pg/, the only one the role may
# list) with a delimiter, so only the prefixes come back.
nightly_dates() {
	local listing
	listing=$(aws s3api list-object-versions --bucket "$BACKUP_BUCKET" --prefix "pg/" \
		--delimiter "/" --query "CommonPrefixes[].Prefix" --output text) || return 1
	printf '%s\n' "$listing" | tr '\t' '\n' | sed -nE 's#^pg/([0-9]{4}-[0-9]{2}-[0-9]{2})/$#\1#p' | sort -u
}

# The night dates of every stored dump of <database> (versions only, not
# delete markers), one per line.
dump_dates() {
	local database=$1 listing
	listing=$(aws s3api list-object-versions --bucket "$BACKUP_BUCKET" --prefix "pg/" \
		--query "Versions[?ends_with(Key, '/${database}.dump')].Key" --output text) || return 1
	printf '%s\n' "$listing" | tr '\t' '\n' | sed -nE 's#^pg/([0-9]{4}-[0-9]{2}-[0-9]{2})/.*#\1#p' | sort -u
}

# Days from <earlier YYYY-MM-DD> to <later YYYY-MM-DD>. In python3, not
# `date -d`: deploy scripts stay portable (test_deploy_script_failures).
days_between() {
	python3 -c 'import sys, datetime as d; f = d.date.fromisoformat; print((f(sys.argv[2]) - f(sys.argv[1])).days)' "$1" "$2"
}

# erase_one <database> <organisation id>: 0 when every version is gone and the
# closure is marked cleared. REMOVED_VERSIONS counts what it deleted.
REMOVED_VERSIONS=0
erase_one() {
	local database=$1 org=$2 versions leftover key version
	if ! versions=$(list_versions "$database"); then
		echo "  listing failed (AWS error above); not marked." >&2
		return 1
	fi
	while IFS=$'\t' read -r key version; do
		[ -n "$key" ] || continue
		if ! aws s3api delete-object --bucket "$BACKUP_BUCKET" --key "$key" \
			--version-id "$version" >/dev/null; then
			echo "  a version could not be deleted (AWS error above); not marked." >&2
			return 1
		fi
		REMOVED_VERSIONS=$((REMOVED_VERSIONS + 1))
	done <<<"$versions"
	# The record this marks is the evidence the DPA § 13 deletion happened, so
	# it rests on a re-listing, not on the loop having run: a loop that ran
	# zero times looks exactly like one that had nothing to do.
	if ! leftover=$(list_versions "$database"); then
		echo "  re-listing failed (AWS error above); not marked." >&2
		return 1
	fi
	if [ -n "$leftover" ]; then
		echo "  $(printf '%s\n' "$leftover" | grep -c .) version(s) still present after deletion; not marked." >&2
		return 1
	fi
	if ! ssm_run "$MARK_DOCUMENT" "OrganizationId=${org},RunId=${GITHUB_RUN_ID}"; then
		echo "  dumps erased, but marking the closure cleared failed; the next run re-verifies and marks it." >&2
		return 1
	fi
}

erase() {
	[ -n "${BACKUP_BUCKET:-}" ] || die "BACKUP_BUCKET is not set (the BACKUP_S3_BUCKET secret)."
	[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]{1,20}$ ]] || die "GITHUB_RUN_ID is not a run number."
	ssm_run "$OWED_DOCUMENT" || die "could not read the erasure queue from the VM."
	local json total
	json=$(queue_json)
	[ -n "$json" ] || die "the queue printed no FEOH_ERASURE line."
	jq -e '.owed | type == "array"' >/dev/null <<<"$json" || die "the queue is not the expected shape."

	# Mask EVERY identifier before using any, so no later line — ours or an AWS
	# error quoting a key — can print one into this public log.
	local field
	while IFS= read -r field; do
		[ -z "$field" ] || echo "::add-mask::${field}"
	done < <(jq -r '.owed[] | .organization_id, .slug, .database' <<<"$json")

	total=$(jq '.owed | length' <<<"$json")
	local all
	all=$(jq '.total // (.owed | length)' <<<"$json")
	is_count "$all" || die "the queue is not the expected shape."
	if [ "$all" -gt "$total" ]; then
		echo "${all} workspace(s) are owed; this run takes the oldest ${total} (the SSM output cap). Later runs take the rest."
	fi

	# (a) Backups are running: the newest night is recent. Without that, the
	# "still being dumped" check in (b) proves nothing, so nothing is erased.
	local dates newest today recent
	dates=$(nightly_dates) || die "could not list the nightly backup dates (AWS error above); nothing erased."
	newest=$(printf '%s\n' "$dates" | grep . | tail -1 || true)
	[ -n "$newest" ] || die "no nightly backup prefix under pg/; nothing erased."
	today=$(date -u +%F)
	if [ "$(days_between "$newest" "$today")" -gt "$MAX_BACKUP_AGE_DAYS" ]; then
		die "the newest nightly backup is older than ${MAX_BACKUP_AGE_DAYS} days, so a live tenant could not be told from a removed one; nothing erased. Fix backup.sh first."
	fi
	recent=$(printf '%s\n' "$dates" | grep . | tail -"$RECENT_NIGHTS")
	echo "Newest nightly backup: $(days_between "$newest" "$today") day(s) old."

	local i org database blocked closed age dumps after erased=0 failed=0 skipped=0 deferred=0
	for ((i = 0; i < total; i++)); do
		org=$(jq -r ".owed[$i].organization_id" <<<"$json")
		database=$(jq -r ".owed[$i].database" <<<"$json")
		blocked=$(jq -r ".owed[$i].blocked // empty" <<<"$json")
		closed=$(jq -r ".owed[$i].closed_on // empty" <<<"$json")
		age=$(jq -r ".owed[$i].age_days // empty" <<<"$json")
		echo "Workspace $((i + 1)) of ${total}:"
		if [ -n "$blocked" ]; then
			# A live organisation holds the slug, or the database still exists:
			# erasing now would hit live backups or be undone tonight. A human
			# decides (docs/founder-runbooks/workspace-closure.md).
			echo "  blocked (${blocked}); not erased." >&2
			skipped=$((skipped + 1))
			continue
		fi
		if ! [[ "$org" =~ $UUID_RE ]] || ! [[ "$database" =~ $DATABASE_RE ]] ||
			! [[ "$closed" =~ $DATE_RE ]] || ! is_count "$age"; then
			echo "  not a well-formed queue entry; not erased." >&2
			failed=$((failed + 1))
			continue
		fi
		if ! dumps=$(dump_dates "$database"); then
			echo "  listing failed (AWS error above); not erased." >&2
			failed=$((failed + 1))
			continue
		fi
		# (b) Dumped after it was removed: it was there to dump. A forged
		# entry or a failed removal — never erase, and say so in red.
		after=$(printf '%s\n' "$dumps" | grep . | awk -v c="$closed" '$0 > c' | head -1 || true)
		if [ -n "$after" ]; then
			echo "::error::  backed up AFTER its recorded removal (a night $(days_between "$closed" "$after") day(s) later): forged entry or failed removal; not erased." >&2
			failed=$((failed + 1))
			continue
		fi
		# (c) Cool-off.
		if [ "$age" -lt "$COOL_OFF_DAYS" ]; then
			echo "  removed ${age} day(s) ago; deferred until ${COOL_OFF_DAYS}."
			deferred=$((deferred + 1))
			continue
		fi
		# (b) Restore window: still in one of the newest nights.
		if printf '%s\n' "$dumps" | grep -qxF -f <(printf '%s\n' "$recent" | grep .); then
			echo "  still in one of the newest ${RECENT_NIGHTS} nightly backups; deferred."
			deferred=$((deferred + 1))
			continue
		fi
		if erase_one "$database" "$org"; then
			echo "  erased and marked cleared."
			erased=$((erased + 1))
		else
			failed=$((failed + 1))
		fi
	done

	local line="Erased ${erased} of ${total} owed workspace(s): ${REMOVED_VERSIONS} object version(s) removed. Deferred: ${deferred}. Failed: ${failed}. Blocked: ${skipped}."
	echo "$line"
	{
		echo "### Backup erasure"
		echo
		echo "$line"
	} >>"$GITHUB_STEP_SUMMARY"
	if [ "$failed" -ne 0 ] || [ "$skipped" -ne 0 ]; then
		die "${failed} failed and ${skipped} blocked; see above. Nothing that failed was marked cleared."
	fi
}

case "${1:-}" in
check) check ;;
erase) erase ;;
*) die "usage: backup-erasure.sh check | erase" ;;
esac
