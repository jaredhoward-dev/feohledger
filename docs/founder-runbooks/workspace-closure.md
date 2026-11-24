# Closed workspaces — erasing the backups every removal leaves

**When you need this**: when the `Backup erasure` workflow asks for an approval
or goes red, and whenever someone asks whether a closed workspace is fully gone.

A workspace is removed in one of two ways: its administrator closes it from
**Organization → Close workspace** (`POST /api/organization/close`,
`docs/decisions.md` §306), or you remove it with `deploy/remove-tenant.sh
<slug>`. Either way the workspace's documents, database and control-plane rows
are deleted then and there. Its nightly **backup dumps** are not: nothing on the
VM may delete a backup (`docs/decisions.md` §490), so a compromised VM cannot
wipe the backup history.

`/legal/dpa` § 13 promises those dumps go within **60 days**. Every removal
leaves a `workspace_closures` row that says so, and the **`Backup erasure`**
GitHub Actions workflow (`.github/workflows/backup-erasure.yml`) works through
them.

## The erasure workflow

- **Every day** its `check` job counts the queue on the VM and prints counts
  only (the log is public). Nothing is deleted.
- **When anything is owed**, its `erase` job waits for your approval (it runs
  in the `production` environment). GitHub emails you about the pending
  deployment. Approve it in the run's page, or with
  `gh run list --workflow backup-erasure.yml --repo jaredhoward-dev/feohledger`
  and the run's **Review deployments** button.
- **Once approved**, for each owed workspace it deletes every version and every
  delete marker of `pg/*/feoh_<slug>.dump`, lists the bucket again and requires
  none to be left, and only then marks the row cleared
  (`backups_cleared_by = github-actions-run-<run id>`). A workspace that fails
  anywhere is not marked and the job goes red; the next run tries it again.
- **The `attention` job goes red** when an owed erasure is 45 days old (inside
  the DPA's 60) or one is blocked. A red run emails you.

To run it now rather than wait for the schedule:

```bash
gh workflow run backup-erasure.yml --repo jaredhoward-dev/feohledger
```

## The queue, by hand

```bash
# on the VM (aws ssm start-session --target <app_instance_id> --profile feohledger)
docker compose … exec -T api python scripts/workspace_closures.py list
```

prints every removed workspace whose backups are not yet cleared, oldest first,
with its organisation id, and closures that began and never finished.

**Telling the customer.** `remove-tenant.sh` prints the written confirmation of
the live-data deletion. Send the backup confirmation once `list` no longer
shows the organisation (the row reads cleared).

**Until a row is cleared the slug stays taken**, and for 90 days from the
removal regardless (`utils/slug.CLOSED_SLUG_COOLDOWN`): a new workspace under
it would get the same `feoh_<slug>` database name — so erasing the old dumps
would erase the new tenant's — and old bookmarks, IdP redirect URIs and invite
emails would point at it. The hold is in `provision_tenant`, so
`scripts/create_tenant.py` and `deploy/add-tenant.sh` refuse a held slug too
(exit 2).

## When the workflow says "blocked"

The erase job refuses a workspace, for a human to decide, when:

- **`slug_in_use`** — an organisation holds the slug again. The hold above should
  make that impossible; erasing would delete the live tenant's backups. Find out
  how the slug was reused before doing anything.
- **`database_exists`** — the tenant database still exists although its
  organisation is gone, so tonight's backup would dump it again. A removal drops
  the database first, so something recreated it (a restore, say). Find out what,
  drop it once you are sure nothing live is in it (`deploy/psql.sh`), and the
  next run erases the dumps.

The public log names neither; `list` on the VM shows which organisation it is.

## When the workflow cannot run

If the erasure workflow is broken and the 60 days are close, the dumps can be
removed from an operator SSO session (`AWS_PROFILE=feohledger`) with the same
listing the job uses (`deploy/backup-erasure.sh`, `list_versions`: each list
filtered separately, the `/`-anchored suffix), re-listed to empty, then marked
with `scripts/workspace_closures.py mark-cleared <organization-id> --operator
<your-handle>`. Exit code 4 means no finished closure of that organisation is
still owed (already marked, mistyped, or a workspace that still exists — check
`list`). Fix the workflow afterwards; it is the record that the deletion
happened under review.

## An ERP grant the closure could not revoke

A closure reads the workspace's ERP OAuth grant before the control-plane rows
go and revokes it at the provider just after the closure commits, so the
provider's latency never holds the closure's row locks (`docs/decisions.md`
§371). It never stops for it. The API log has two lines for it: one when the
revoke is queued, and the outcome once the provider answers:

```
[tenant-deletion] <slug>: ERP OAuth grant (<provider>) to be revoked once the closure commits
[tenant-deletion] <slug>: ERP OAuth grant (<provider>) revoked|failed|unsupported|unreadable
```

A queued line with no outcome line after it (the API restarted in between)
counts as not revoked.

Anything but `revoked` (a warning) means the grant may still be live at the
provider. Tell the customer to remove FeohLedger from that provider's connected
apps; for Blackbaud (`unsupported`, SKY API has no revocation endpoint) that is
disconnecting it from the environment in Blackbaud Marketplace. An operator
deletion (`scripts/delete_tenant.py`) prints the same outcome in its written
confirmation.

An ERP connected with stored credentials instead of a consent logs the same two
lines as `ERP credentials (<provider>)` (§400). For Sage Intacct a `revoked`
outcome ends every token Intacct issued us, but the company still lists our
client ID: tell the customer to remove it under Company > Setup > Company >
Security > Authorized Client Applications. Intacct's revoke ends every token for
that Web Services user and company, so anything else sharing the user (another
integration of the customer's) must sign in again. Sage Accounting (ZA) issues no token,
so it always reads `unsupported`: tell the customer to change the password of
the Sage user whose login FeohLedger held, or remove that user. NetSuite (§406)
depends on the sign-in method: a TBA token is revoked through Oracle's
`revoketoken` and reads `revoked` only once that token is refused, so on
anything else tell the customer to revoke it under Setup > Users/Roles > User
Management > Access Tokens; an OAuth 2.0 client-credentials connection always
reads `unsupported` (no endpoint revokes it), so tell them to revoke our
certificate under Setup > Integration > Manage Authentication > OAuth 2.0
Client Credentials (M2M) Setup. Business Central and SYSPRO always read
`unsupported` (§407). For Business Central, Microsoft publishes no revocation
for a client-credentials token: tell the customer to set our client ID to
Disabled (or delete it) on BC's Microsoft Entra Applications page and delete the
client secret they gave us from that app registration in the Microsoft Entra
admin center; tokens already issued lapse within 60–90 minutes. For SYSPRO,
e.net's `Logoff` ends a session, not the operator, and we log off after every
call: tell the customer to set Operator locked out (or Usage for e.net business
objects to No access) on the operator we logged on as in Operator Maintenance,
or change its password. For Merge.dev
a `revoked` outcome means Merge deleted the linked account (§405); a `failed`
one means the customer must delete it. Either way the API key and the webhook
are the customer's own Merge organisation's: tell them to delete the webhook
that points at FeohLedger, regenerate the webhook signature key, and regenerate
or delete the API key FeohLedger held. Until they do, the webhook keeps calling
the deleted slug's URL, and those deliveries are dropped. The operator's
written confirmation names the step in each case.

## What stays, on purpose

- **The `workspace_closures` row** — organisation id, slug, the requesting
  user's id (which no longer resolves; none for a removal you ran, whose proof
  reads `operator`), the step-up proof, what was removed and when, and who
  cleared the backups. It holds no address or name. It is the record that the
  deletion was asked for and that it happened, and it is what this queue reads.
- **Audit events already shipped to write-once archival.** Where audit shipping
  is on, the closure ships the workspace's whole trail, including its
  `organization.closing` event, before it deletes anything. Those copies cannot
  be deleted before their retention expires, as the DPA discloses.
- **The shared control-plane dump**, which is not selectively editable and ages
  out on the 90-day backup cycle, as the DPA discloses.

## When a closure did not finish

A closure that failed part-way answers the administrator with "did not finish,
try again", and a retry by them completes it. If they have given up, `list`
shows it under "began and did not finish", with the exception class of the
last failure. The database goes first, so a failed drop leaves the workspace
whole; a failure after the drop leaves a workspace that is already unusable.
Finish it with the ordinary operator deletion (`deploy/remove-tenant.sh
<slug>`, which is idempotent): it completes the same row, keeping the
administrator as its requester, and the backups join the erasure queue.

## When a customer asks support to close a workspace the page refused

The page refuses any workspace holding records that must be kept (approved
invoices, payments, other users, a paid subscription and the rest of the list
in `docs/user-management.md`). That is the ordinary DPA deletion: confirm the
request is from the customer, offer the export the DPA promises, then
`deploy/remove-tenant.sh <slug>`; its backups join the erasure queue like a
closure's. Loop in the CISO before deleting a workspace with financial records.
