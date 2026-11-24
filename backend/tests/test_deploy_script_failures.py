"""Every deploy script production runs fails non-zero when a step it depends on
fails — and the few steps it deliberately skips are skipped visibly (#575).

`decrypt-env.sh`, `deploy.sh`, `publish-frontend.sh` and `smoke-frontend.sh`
have their own files (`test_deploy_decrypt_env.py`, `test_deploy_release.py`,
`test_frontend_publish.py`); this one covers the rest: `add-tenant.sh`,
`remove-tenant.sh`, `backup-erasure-queue.sh`, `backup-erasure.sh`, `backup.sh`,
`restore.sh`, `compose.sh`, `psql.sh` and `bootstrap-vm.sh`.

Same shape as those files: the real script, copied with `lib.sh` into a scratch
`deploy/`, runs against stubs placed first on PATH. One stub serves every tool
(`docker`, `aws`, `curl`, `sudo`, …): it appends its argv to `calls` and answers
from a rules file — `tool|substring|exit code|output` — so a test states only
the call that should fail. Nothing reaches Docker, AWS or a database.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"

STUB = r"""#!/bin/sh
name=${0##*/}
printf '%s %s\n' "$name" "$*" >> "$STUB_DIR/calls"
# A stub at the end of a pipe (`aws s3 cp - …`, `sudo tee …`) reads its stdin
# as the real tool would, so the writer never dies of SIGPIPE.
case " $* " in *" - "* | " tee "*) cat >/dev/null ;; esac
if [ -f "$STUB_DIR/rules" ]; then
	while IFS='|' read -r tool pattern rc out; do
		[ "$tool" = "$name" ] || continue
		case "$*" in
		*"$pattern"*)
			# A rule whose rc ends in "!" answers once: the first matching call
			# gets it, later ones fall through to the next rule (or exit 0).
			case "$rc" in *!)
				mark="$STUB_DIR/once.$(printf '%s' "$tool|$pattern" | cksum | cut -d' ' -f1)"
				[ -e "$mark" ] && continue
				: > "$mark"
				rc=${rc%!} ;;
			esac
			[ -z "$out" ] || printf '%b' "$out"
			exit "$rc"
			;;
		esac
	done < "$STUB_DIR/rules"
fi
exit 0
"""

LOCAL_ENV = "POSTGRES_PASSWORD=x\nAPP_DOMAIN=app.example.com\nBACKUP_S3_BUCKET=feoh-backups\n"
EXTERNAL_ENV = (
    "FEOH_DATABASE_URL=postgresql+asyncpg://feohadmin:pw@db.example.internal:5432/feohledger\n"
    "APP_DOMAIN=app.example.com\nBACKUP_S3_BUCKET=feoh-backups\nAWS_REGION=us-east-1\n"
)


class Deploy:
    """A scratch `deploy/` holding one script, `lib.sh`, a `.env`, and stubs."""

    def __init__(self, root: Path, script: str, tools: tuple[str, ...]) -> None:
        self.root = root
        self.dir = root / "deploy"
        self.dir.mkdir()
        self.script = self.dir / script
        shutil.copy2(DEPLOY / script, self.script)
        shutil.copy2(DEPLOY / "lib.sh", self.dir / "lib.sh")
        (self.dir / "Caddyfile").write_text("# stub\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        for tool in tools:
            stub = self.bin / tool
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.rules: list[str] = []

    def env_file(self, text: str) -> None:
        (self.dir / ".env").write_text(text)

    def rule(
        self, tool: str, pattern: str, rc: int = 0, out: str = "", *, once: bool = False
    ) -> None:
        """``once`` answers only the first matching call, e.g. a backup listing
        that a successful deletion empties before the script lists it again."""
        self.rules.append(f"{tool}|{pattern}|{rc}{'!' if once else ''}|{out}")

    def run(
        self, *args: str, path: str | None = None, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        (self.root / "rules").write_text("\n".join(self.rules) + "\n")
        (self.root / "calls").write_text("")
        return subprocess.run(
            ["/bin/bash", str(self.script), *args],
            env={
                "PATH": path or f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "STUB_DIR": str(self.root),
                "HOME": str(self.root),
                **(extra_env or {}),
            },
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return (self.root / "calls").read_text().splitlines()

    def called(self, needle: str) -> bool:
        return any(needle in c for c in self.calls())


def _deploy(tmp_path: Path, script: str, *tools: str) -> Deploy:
    return Deploy(tmp_path, script, tools or ("docker", "aws", "curl"))


# ── add-tenant.sh ────────────────────────────────────────────────────────────

ADD_ARGS = ("acme", "--name", "Acme Corp", "--admin-email", "admin@acme.test")


def test_add_tenant_fails_when_provisioning_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "add-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "scripts/create_tenant.py", 1)

    result = d.run(*ADD_ARGS)

    assert result.returncode != 0
    assert "tenant ready" not in result.stdout


def test_add_tenant_succeeds_end_to_end_against_the_stubs(tmp_path: Path) -> None:
    """The control case for the one above: the same stubs, nothing failing. The
    tenant host is CloudFront's wildcard (docs/decisions.md §364), so Caddy is
    neither written to nor reloaded."""
    d = _deploy(tmp_path, "add-tenant.sh")
    d.env_file(LOCAL_ENV)

    result = d.run(*ADD_ARGS)

    assert result.returncode == 0, result.stderr
    assert "tenant ready: https://acme.app.example.com" in result.stdout
    assert not (d.dir / "tenants.caddy").exists()
    assert not d.called("caddy")


@pytest.mark.parametrize("env", [None, "POSTGRES_PASSWORD=x\n"])
def test_add_tenant_refuses_without_an_env_or_an_app_domain(tmp_path: Path, env) -> None:
    d = _deploy(tmp_path, "add-tenant.sh")
    if env is not None:
        d.env_file(env)

    result = d.run(*ADD_ARGS)

    assert result.returncode != 0
    assert not d.called("create_tenant.py")


# ── remove-tenant.sh ─────────────────────────────────────────────────────────
#
# Since docs/decisions.md §490 it never touches the backup bucket: the VM may
# not delete a backup. delete_tenant.py records the removal, and the
# approval-gated backup-erasure workflow erases the dumps.


@pytest.mark.parametrize(
    ("rc", "why"),
    [
        (1, "the api container is down / the database is unreachable"),
        (2, "delete_tenant.py refused (a partner organisation with children)"),
    ],
)
def test_remove_tenant_stops_when_the_inventory_fails(tmp_path: Path, rc: int, why: str) -> None:
    """The #575 silent-skip finding: ANY failed inventory used to be read as
    "already deleted". Only exit 4 (removed, on record) means that now."""
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "delete_tenant.py --slug acme --dry-run", rc)

    result = d.run("acme", "--yes")

    assert result.returncode != 0, why
    assert "inventory failed" in result.stderr
    assert not d.called("--yes"), "the tenant deletion ran"
    assert "removed." not in result.stdout


def test_remove_tenant_with_nothing_on_record_is_refused(tmp_path: Path) -> None:
    """Exit 5: no organisation and no removal on record — most likely a typo."""
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "--dry-run", 5)

    result = d.run("acme", "--yes")

    assert result.returncode != 0
    assert "nothing found" in result.stderr


def test_remove_tenant_already_removed_stops_cleanly(tmp_path: Path) -> None:
    """Exit 4: removed and on record. There is no backup leg left to finish
    here, so it says so and changes nothing."""
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "--dry-run", 4)

    result = d.run("acme", "--yes")

    assert result.returncode == 0, result.stderr
    assert "already removed" in result.stdout
    assert not d.called("delete_tenant.py --slug acme --yes")


def test_remove_tenant_never_touches_caddy(tmp_path: Path) -> None:
    """A platform tenant host is CloudFront's wildcard (docs/decisions.md §364):
    a vanity block in tenants.caddy is left for the operator, and the script
    says so, rather than being guessed at."""
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    vanity = "shop.acmecorp.com {\n\timport vanity\n}\n"
    (d.dir / "tenants.caddy").write_text(vanity)

    result = d.run("acme", "--yes")

    assert result.returncode == 0, result.stderr
    assert (d.dir / "tenants.caddy").read_text() == vanity
    assert not d.called("caddy")
    assert "vanity domain" in result.stdout


def test_remove_tenant_never_deletes_a_backup(tmp_path: Path) -> None:
    """Nothing the VM can trigger on its own may delete a backup (§490): no AWS
    call at all, the removal queued for the erasure workflow, and no line that
    reads as a backup deletion confirmed."""
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)

    result = d.run("acme", "--yes")

    assert result.returncode == 0, result.stderr
    assert not d.called("aws ")
    assert d.called("delete_tenant.py --slug acme --yes")
    assert not d.called("--no-backup-store")
    assert "backup-erasure workflow" in result.stdout
    assert "removed from s3://" not in result.stdout
    assert "verified empty" not in result.stdout


def test_remove_tenant_without_a_backup_store_owes_nothing(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file("POSTGRES_PASSWORD=x\nAPP_DOMAIN=app.example.com\n")

    result = d.run("acme", "--yes")

    assert result.returncode == 0, result.stderr
    assert d.called("delete_tenant.py --slug acme --yes --no-backup-store")
    assert "no backup store configured" in result.stdout


def test_remove_tenant_stops_when_the_data_deletion_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "remove-tenant.sh")
    d.env_file(LOCAL_ENV)
    d.rule("docker", "delete_tenant.py --slug acme --yes", 1)

    result = d.run("acme", "--yes")

    assert result.returncode != 0
    assert "removed." not in result.stdout


# ── backup-erasure-queue.sh (the VM's half, run by the SSM documents) ─────────

ORG = "0b5e8f9a-1c2d-4e3f-8a9b-0c1d2e3f4a5b"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("summary",), "workspace_closures.py owed --summary"),
        (("owed",), "workspace_closures.py owed --json"),
        (
            ("mark-cleared", ORG, "12345"),
            f"workspace_closures.py mark-cleared {ORG} --operator github-actions-run-12345",
        ),
    ],
)
def test_erasure_queue_runs_the_closures_script_in_the_api(
    tmp_path: Path, args: tuple[str, ...], expected: str
) -> None:
    d = _deploy(tmp_path, "backup-erasure-queue.sh")
    d.env_file(LOCAL_ENV)

    result = d.run(*args)

    assert result.returncode == 0, result.stderr
    assert d.called("exec -T api python scripts/" + expected)
    assert not d.called("aws ")


@pytest.mark.parametrize(
    "args",
    [
        ("mark-cleared", "acme", "12345"),
        ("mark-cleared", ORG.upper(), "12345"),
        ("mark-cleared", ORG, "12a"),
        ("mark-cleared", ORG),
        ("summary", "extra"),
        ("delete",),
    ],
)
def test_erasure_queue_refuses_anything_else(tmp_path: Path, args: tuple[str, ...]) -> None:
    d = _deploy(tmp_path, "backup-erasure-queue.sh")
    d.env_file(LOCAL_ENV)

    result = d.run(*args)

    assert result.returncode != 0
    assert not d.called("workspace_closures.py")


# ── backup-erasure.sh (the runner's half, .github/workflows/backup-erasure.yml) ──


ORG_B = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


def _erasure(tmp_path: Path, queue: str) -> Deploy:
    """The runner script against stubbed `aws`: every SSM command succeeds and
    returns ``queue`` as its output, unless a test adds a rule first."""
    d = _deploy(tmp_path, "backup-erasure.sh", "aws")
    d.queue = queue  # type: ignore[attr-defined]
    return d


def _night(days_ago: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def _nights(*days_ago: int) -> str:
    """A `CommonPrefixes[].Prefix` text answer: tab-separated date prefixes."""
    return "\\t".join(f"pg/{_night(n)}/" for n in days_ago) + "\\n"


def _listing(database: str) -> str:
    """The rule pattern that matches only the erase listing (every version and
    delete marker) of ``database`` — not its dump-dates listing, not a delete."""
    return f"/{database}.dump')]][]"


def _dates(database: str) -> str:
    """The rule pattern for the dump-dates listing (versions only) of ``database``."""
    return f"/{database}.dump')].Key"


def _run_erasure(d: Deploy, *args: str) -> subprocess.CompletedProcess[str]:
    # After the test's own rules, so a test's rule for a call wins. By default
    # the nightly backups are current (last night and the two before).
    d.rule("aws", "--delimiter", 0, _nights(10, 2, 1, 0))
    d.rule("aws", "send-command", 0, "cmd-1\\n")
    d.rule("aws", "--query Status", 0, "Success\\n")
    d.rule("aws", "StandardOutputContent", 0, d.queue)  # type: ignore[attr-defined]
    out = d.root / "github_output"
    out.write_text("")
    return d.run(
        *args,
        extra_env={
            "INSTANCE_ID": "i-0123",
            "BACKUP_BUCKET": "feoh-backups",
            "GITHUB_RUN_ID": "987",
            "GITHUB_OUTPUT": str(out),
            "SSM_POLL_SECONDS": "0",
        },
    )


def _queue(*entries: tuple[str, str, str | None], age: int = 30, total: int | None = None) -> str:
    import json

    owed = [
        {
            "organization_id": org,
            "slug": slug,
            "database": f"feoh_{slug}",
            "closed_on": _night(age),
            "age_days": age,
            "blocked": b,
        }
        for org, slug, b in entries
    ]
    body = {"owed": owed, "total": len(owed) if total is None else total}
    # printf %b in the stub: no backslashes in the JSON, and no `|` (the rule separator).
    return "booting\\nFEOH_ERASURE " + json.dumps(body) + "\\n"


def _not_masked(result: subprocess.CompletedProcess[str], *values: str) -> list[str]:
    """Lines that show a value without a preceding ::add-mask:: of it."""
    masked: set[str] = set()
    leaks = []
    for line in (result.stdout + result.stderr).splitlines():
        if line.startswith("::add-mask::"):
            masked.add(line.removeprefix("::add-mask::"))
            continue
        leaks += [f"{v}: {line}" for v in values if v in line and v not in masked]
    return leaks


def test_erasure_check_writes_counts_and_names_nothing(tmp_path: Path) -> None:
    summary = (
        '{"blocked": 1, "cool_off_days": 7, "cooling": 0, "oldest_age_days": 50, "overdue": 1, '
        '"overdue_after_days": 45, "owed": 3, "ready": 2}'
    )
    d = _erasure(tmp_path, f"FEOH_ERASURE {summary}\\n")

    result = _run_erasure(d, "check")

    assert result.returncode == 0, result.stderr
    assert (d.root / "github_output").read_text() == "ready=2\nblocked=1\noverdue=1\n"
    assert d.called("--document-name feohledger-backup-erasure-summary")
    assert not d.called("s3api"), "the check role has no S3 access"


def test_erasure_check_fails_on_a_summary_it_cannot_read(tmp_path: Path) -> None:
    d = _erasure(tmp_path, "Traceback (most recent call last)\\n")

    result = _run_erasure(d, "check")

    assert result.returncode != 0
    assert (d.root / "github_output").read_text() == ""


def test_erasure_deletes_every_version_verifies_and_marks(tmp_path: Path) -> None:
    d = _erasure(tmp_path, _queue((ORG, "acme", None)))
    listing = "pg/2026-11-01/feoh_acme.dump\\tv1\\npg/2026-11-02/feoh_acme.dump\\tv2\\n"
    # The first listing finds two versions; the re-listing after the deletes, none.
    d.rule("aws", _listing("feoh_acme"), 0, listing, once=True)

    result = _run_erasure(d, "erase")

    assert result.returncode == 0, result.stderr
    assert d.called("--key pg/2026-11-01/feoh_acme.dump --version-id v1")
    assert d.called("--key pg/2026-11-02/feoh_acme.dump --version-id v2")
    assert d.called("--document-name feohledger-backup-erasure-mark-cleared --instance-ids i-0123")
    assert d.called(f"--parameters OrganizationId={ORG},RunId=987")
    assert "2 object version(s) removed" in result.stdout
    # Listing is always under exactly `pg/` — the only prefix the role may list.
    lists = [c for c in d.calls() if "list-object-versions" in c]
    assert lists and all("--prefix pg/ " in c for c in lists)
    for value in (ORG, "acme", "feoh_acme"):
        assert f"::add-mask::{value}" in result.stdout.splitlines()
    assert not _not_masked(result, ORG, "acme", "feoh_acme")


def test_erasure_that_leaves_a_version_behind_is_not_marked(tmp_path: Path) -> None:
    d = _erasure(tmp_path, _queue((ORG, "acme", None), (ORG_B, "beta", None)))
    d.rule("aws", _listing("feoh_acme"), 0, "pg/2026-11-01/feoh_acme.dump\\tv1\\n")
    d.rule("aws", _listing("feoh_beta"), 0, "pg/2026-11-01/feoh_beta.dump\\tv9\\n", once=True)

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert "still present" in result.stderr
    assert not d.called(f"OrganizationId={ORG},")
    # One workspace's failure does not stop the next.
    assert d.called(f"OrganizationId={ORG_B},")
    assert "Erased 1 of 2" in result.stdout
    assert not _not_masked(result, ORG, ORG_B, "acme", "beta")


def test_erasure_that_cannot_delete_is_not_marked(tmp_path: Path) -> None:
    d = _erasure(tmp_path, _queue((ORG, "acme", None)))
    d.rule("aws", _listing("feoh_acme"), 0, "pg/2026-11-01/feoh_acme.dump\\tv1\\n", once=True)
    d.rule("aws", "delete-object", 254, "An error occurred (AccessDenied)\\n")

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert not d.called("mark-cleared")


def test_erasure_skips_a_blocked_workspace_and_fails_for_a_human(tmp_path: Path) -> None:
    d = _erasure(tmp_path, _queue((ORG, "acme", "slug_in_use")))

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert "slug_in_use" in result.stderr
    # Only the bucket-wide freshness listing ran; nothing of this tenant's.
    assert not d.called("feoh_acme")
    assert not d.called("delete-object")
    assert not d.called("mark-cleared")


def test_erasure_refuses_a_database_name_that_is_not_a_tenant_dump(tmp_path: Path) -> None:
    """`feohledger` is the shared control-plane dump; IAM denies it too."""
    import json

    queue = "FEOH_ERASURE " + json.dumps(
        {
            "owed": [
                {
                    "organization_id": ORG,
                    "slug": "x",
                    "database": "feohledger",
                    "closed_on": _night(30),
                    "age_days": 30,
                    "blocked": None,
                }
            ]
        }
    )
    d = _erasure(tmp_path, queue + "\\n")

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert not d.called("feohledger.dump")
    assert not d.called("delete-object")
    assert not d.called("mark-cleared")


def test_erasure_with_a_failed_mark_reports_failure(tmp_path: Path) -> None:
    d = _erasure(tmp_path, _queue((ORG, "acme", None)))
    d.rule("aws", "send-command --document-name feohledger-backup-erasure-mark-cleared", 254)

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert "marking the closure cleared failed" in result.stderr


# ── backup.sh ────────────────────────────────────────────────────────────────

DB_LISTING = "feohledger\\nfeoh_acme\\n"


def _backup(tmp_path: Path) -> Deploy:
    d = _deploy(tmp_path, "backup.sh")
    d.env_file(LOCAL_ENV)
    return d


def test_backup_dumps_every_database_and_pings_the_heartbeat(tmp_path: Path) -> None:
    d = _backup(tmp_path)
    d.rule("docker", "SELECT datname", 0, DB_LISTING)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode == 0, result.stderr
    assert "(local database)" in result.stdout and "2 databases" in result.stdout
    assert d.called("pg_dump -U postgres -Fc -d feohledger")
    assert d.called("pg_dump -U postgres -Fc -d feoh_acme")
    assert d.called("curl -fsS -m 10 --retry 3 https://hc.example/ping")


@pytest.mark.parametrize("listing", ["", "feoh_acme\\n"])
def test_backup_without_the_control_plane_in_the_listing_fails(tmp_path: Path, listing) -> None:
    """The #575 silent-skip finding: an empty listing dumped nothing, reported
    "backup complete (0 databases)" and pinged the heartbeat, so the
    dead-man's-switch went on reporting healthy over empty backups."""
    d = _backup(tmp_path)
    d.rule("docker", "SELECT datname", 0, listing)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode != 0
    assert "feohledger" in result.stderr
    assert "backup complete" not in result.stdout
    assert not d.called("hc.example")


@pytest.mark.parametrize(
    ("tool", "pattern"),
    [
        ("docker", "pg_dump -U postgres -Fc -d feoh_acme"),
        ("docker", "pg_dumpall"),
        ("docker", "SELECT datname"),
        ("aws", "feoh_acme.dump"),
    ],
)
def test_backup_fails_when_a_dump_or_an_upload_fails(tmp_path: Path, tool, pattern) -> None:
    d = _backup(tmp_path)
    d.rules.insert(0, f"{tool}|{pattern}|1|")
    d.rule("docker", "SELECT datname", 0, DB_LISTING)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode != 0
    assert "backup complete" not in result.stdout
    assert not d.called("hc.example"), "a failed backup pinged the heartbeat"


def test_backup_without_a_bucket_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "backup.sh")
    d.env_file("POSTGRES_PASSWORD=x\n")

    result = d.run()

    assert result.returncode != 0
    assert "BACKUP_S3_BUCKET" in result.stderr
    assert not d.called("pg_dump")


def test_a_failed_heartbeat_ping_warns_but_the_backup_stands(tmp_path: Path) -> None:
    """The one deliberate non-failure: the dumps are already in S3, and a missed
    ping is exactly what the heartbeat service exists to notice."""
    d = _backup(tmp_path)
    d.rule("docker", "SELECT datname", 0, DB_LISTING)
    d.rule("curl", "hc.example", 22)

    result = d.run(extra_env={"BACKUP_PING_URL": "https://hc.example/ping"})

    assert result.returncode == 0, result.stderr
    assert "WARN: backup heartbeat ping failed" in result.stderr


# ── restore.sh ───────────────────────────────────────────────────────────────
# External (RDS) mode, so the role-globals step — a gunzip of the dump, which an
# empty stub cannot stand in for — is not part of the run.

DUMP_LISTING = "2026-10-19 03:17:00 1234 feohledger.dump\\n"


def _restore(tmp_path: Path) -> Deploy:
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(EXTERNAL_ENV)
    d.rule("aws", "s3 ls", 0, DUMP_LISTING)
    return d


def test_restore_skips_an_existing_database_without_force_and_says_so(tmp_path: Path) -> None:
    """The documented skip: an existing database is left alone unless --force,
    and the run says which — then still rolls the stack back up."""
    d = _restore(tmp_path)
    d.rule("docker", "datname = 'feohledger'", 0, "1\\n")

    result = d.run("2026-10-19")

    assert result.returncode == 0, result.stderr
    assert "feohledger: already exists — skipped" in result.stdout
    assert not d.called("pg_restore")
    assert d.called("up -d --wait")


def test_restore_fails_when_pg_restore_fails(tmp_path: Path) -> None:
    d = _restore(tmp_path)
    d.rule("docker", "pg_restore", 1)

    result = d.run("2026-10-19", "--force")

    assert result.returncode != 0
    assert "restore complete" not in result.stdout


IAM_ENV = (
    "FEOH_DATABASE_URL=postgresql+asyncpg://feoh_app@db.example.internal:5432/feohledger\n"
    "FEOH_DATABASE_IAM_AUTH=true\nPGSSLMODE=verify-full\n"
    "APP_DOMAIN=app.example.com\nBACKUP_S3_BUCKET=feoh-backups\nAWS_REGION=us-east-1\n"
)


def test_restore_refuses_the_iam_app_role_before_touching_anything(tmp_path: Path) -> None:
    """The app role cannot recreate pgvector, so a logical restore is the
    master's (docs/decisions.md §310)."""
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(IAM_ENV)

    result = d.run("2026-10-19")

    assert result.returncode != 0
    assert "db-role-cutover.sh" in result.stderr and "master" in result.stderr
    assert d.calls() == [], "a refused restore still ran a tool"


def test_a_break_glass_restore_runs_as_the_master_and_leaves_the_api_stopped(
    tmp_path: Path,
) -> None:
    """With the master URL in the shell for this one run, the restore proceeds —
    and never rolls the api back up, which would hand it that master URL."""
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(IAM_ENV)
    d.rule("aws", "s3 ls", 0, DUMP_LISTING)

    result = d.run(
        "2026-10-19",
        extra_env={
            "FEOH_DATABASE_URL": "postgresql+asyncpg://postgres:pw@db.example.internal/feohledger",
            "FEOH_DATABASE_IAM_AUTH": "false",
        },
    )

    assert result.returncode == 0, result.stderr
    assert d.called("pg_restore")
    assert d.called("stop api")
    assert not d.called("up -d --wait"), "the api came back up on the master's URL"
    assert "./db-role-cutover.sh" in result.stdout and "api is left STOPPED" in result.stdout


def test_restore_with_no_dumps_for_the_date_fails(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(EXTERNAL_ENV)

    result = d.run("2026-10-19")

    assert result.returncode != 0
    assert "no dumps found" in result.stderr
    assert not d.called("stop api")


@pytest.mark.parametrize("stamp", ["", "yesterday", "2026-10-19; rm -rf /"])
def test_restore_refuses_anything_but_a_date(tmp_path: Path, stamp: str) -> None:
    d = _restore(tmp_path)

    result = d.run(*([stamp] if stamp else []))

    assert result.returncode != 0
    assert not d.called("docker")


def test_restore_refuses_an_odd_name_in_the_bucket(tmp_path: Path) -> None:
    d = _deploy(tmp_path, "restore.sh")
    d.env_file(EXTERNAL_ENV)
    d.rule("aws", "s3 ls", 0, "2026-10-19 03:17:00 1 x';drop.dump\\n")

    result = d.run("2026-10-19")

    assert result.returncode != 0
    assert "unexpected database name" in result.stderr


# ── compose.sh / psql.sh ─────────────────────────────────────────────────────


@pytest.mark.parametrize("script", ["compose.sh", "psql.sh"])
def test_the_wrappers_pass_the_tools_failure_through(tmp_path: Path, script: str) -> None:
    d = _deploy(tmp_path, script)
    d.env_file(LOCAL_ENV)
    d.rule("docker", "compose", 3)

    result = d.run("ps")

    assert result.returncode == 3


@pytest.mark.parametrize("script", ["compose.sh", "psql.sh"])
def test_the_wrappers_refuse_without_an_env(tmp_path: Path, script: str) -> None:
    d = _deploy(tmp_path, script)

    result = d.run("ps")

    assert result.returncode != 0
    assert ".env missing" in result.stderr
    assert not d.called("docker")


# ── bootstrap-vm.sh ──────────────────────────────────────────────────────────

BOOTSTRAP_TOOLS = ("sudo", "dnf", "docker", "sops", "aws", "curl", "swapon", "uname")


COMPOSE_PIN = re.search(
    r"^COMPOSE_VERSION=v(\S+)$", (DEPLOY / "bootstrap-vm.sh").read_text(), re.M
)[1]


def _bootstrap(
    tmp_path: Path, *tools: str, compose: str | None = COMPOSE_PIN
) -> tuple[Deploy, str]:
    d = Deploy(tmp_path, "bootstrap-vm.sh", tools)
    # A PATH holding only the stubs and the handful of real tools the script
    # needs, so `command -v dnf` answers for the stub and nothing on this host
    # is ever reached.
    for real in ("dirname", "cat", "grep", "sed"):
        os.symlink(shutil.which(real), d.bin / real)
    d.rule("swapon", "--show", 0, "/swapfile file 2G\\n")
    # The compose plugin the VM already runs (None: none installed). The stub
    # answers the same before and after an install, as a plugin that loses to
    # one earlier on docker's search path would.
    if compose is not None:
        d.rule("docker", "compose version --short", 0, f"{compose}\\n")
    return d, str(d.bin)


def test_bootstrap_refuses_a_distro_without_dnf(tmp_path: Path) -> None:
    d, path = _bootstrap(tmp_path, "sudo")

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode == 1
    assert "targets Amazon Linux 2023" in result.stderr
    assert not d.called("sudo")


def test_bootstrap_fails_when_a_package_install_fails(tmp_path: Path) -> None:
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS)
    d.rule("sudo", "dnf install -y docker git cronie", 1)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode != 0
    assert "bootstrap complete" not in result.stdout
    assert not d.called("systemctl enable --now docker")


def test_bootstrap_off_ec2_skips_the_hop_limit_visibly(tmp_path: Path) -> None:
    """The deliberate skip: no instance metadata (off-EC2) leaves the IMDSv2 hop
    limit alone with a WARN. On EC2 Terraform sets it anyway
    (`infra/compute.tf`, pinned by `infra/tests/guardrails.tftest.hcl`)."""
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS)
    d.rule("curl", "169.254.169.254/latest/api/token", 22)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode == 0, result.stderr
    assert "WARN: no EC2 instance metadata reachable" in result.stderr
    assert "bootstrap complete" in result.stdout
    assert not d.called("modify-instance-metadata-options")


def test_bootstrap_leaves_the_pinned_compose_alone(tmp_path: Path) -> None:
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS)
    d.rule("curl", "169.254.169.254/latest/api/token", 22)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert result.returncode == 0, result.stderr
    assert not d.called("docker/compose/releases/download")


@pytest.mark.parametrize("running", [None, "2.20.0", "5.0.1"])
def test_bootstrap_converges_compose_on_the_pin(tmp_path: Path, running: str | None) -> None:
    """Not merely "some compose is installed": CI checks deploy/ with exactly the
    pinned version (scripts/install_vm_compose.sh), so a VM left on another one
    after a bump runs a compose nothing tests (#569, #575). Here the stub keeps
    answering the old version after the install, so the run must also fail
    loudly instead of reporting a bootstrap that did not take."""
    d, path = _bootstrap(tmp_path, *BOOTSTRAP_TOOLS, compose=running)
    d.rule("curl", "169.254.169.254/latest/api/token", 22)

    result = d.run(path=path, extra_env={"USER": "ec2-user"})

    assert d.called(f"docker/compose/releases/download/v{COMPOSE_PIN}/docker-compose-linux-")
    assert result.returncode != 0
    assert f"installed docker compose v{COMPOSE_PIN}" in result.stderr
    assert "bootstrap complete" not in result.stdout


def test_every_runnable_deploy_script_is_committed_executable() -> None:
    """The operator runs these as `./<script>.sh` on the VM, whose checkout takes
    its file modes from git. `db-role-cutover.sh` shipped as 100644 and answered
    "Permission denied" at the cutover's first step. The index mode is what the VM
    gets, so that is what is checked; `lib.sh` is sourced, never run."""
    listing = subprocess.run(
        ["git", "ls-files", "-s", "--", "deploy/*.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    modes = {line.split("\t", 1)[1]: line.split()[0] for line in listing}
    assert "deploy/db-role-cutover.sh" in modes, "the listing found no deploy scripts"
    not_executable = sorted(
        path for path, mode in modes.items() if mode != "100755" and path != "deploy/lib.sh"
    )
    assert not_executable == []


# ── portability guard ────────────────────────────────────────────────────────
#
# These scripts run on the VM (Linux) and from contributors' Macs, and this
# suite runs them on both: a GNU-only flag makes the tests red on macOS
# (`date: illegal option -- d`). Each pattern is a construct BSD/macOS rejects
# or bash 3.2 lacks; use the portable form named beside it.

_GNU_ISMS = {
    r"\bdate\b[^|;\n]*\s(-d|--date)\b": "relative/parsed dates: compute them in python3",
    r"\bcp\b[^|;\n]*\s(-[a-zA-Z]*[TtrbuvnL]?--|--[a-z])": "GNU long options on cp",
    r"\bcp\s+-[a-zA-Z]*[Tt]": "cp -T / -t (GNU only)",
    r"\bsed\s+(-[a-zA-Z]*\s+)*-i(\s|$|[^a-zA-Z'\"])": (
        "sed -i (needs a suffix on BSD): write a tmp file and mv"
    ),
    r"\breadlink\s+-[a-zA-Z]*f": "readlink -f: use cd + pwd -P",
    r"\bstat\s+(-c|--format)": "stat -c: use wc -c or ls",
    r"\bgrep\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*P": "grep -P: use grep -E",
    r"\bxargs\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*r\b": "xargs -r: guard the empty case instead",
    r"\bmktemp\s+--": "mktemp long options: use -d / a XXXXXX template",
    r"\b(mapfile|readarray)\b": "mapfile (bash 4+; macOS ships 3.2): use a read loop",
    r"\$\(date [^)]*%N": "date %N (GNU only)",
}


def _code_lines(path: Path) -> list[tuple[int, str]]:
    return [
        (n, line)
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


def test_deploy_scripts_use_only_portable_flags() -> None:
    scripts = sorted(DEPLOY.glob("*.sh"))
    assert scripts, "no deploy/*.sh found — the guard would pass vacuously"
    hits = [
        f"{p.name}:{n}: {why}: {line.strip()}"
        for p in scripts
        for n, line in _code_lines(p)
        for pattern, why in _GNU_ISMS.items()
        if re.search(pattern, line)
    ]
    assert not hits, "GNU-only constructs in deploy/:\n" + "\n".join(hits)


@pytest.mark.parametrize(
    "line",
    [
        'cutoff=$(date -u -d "-30 days" +%F)',
        "cp -T a b",
        "cp --archive a b",
        "sed -i 's/a/b/' f",
        "sed -E -i 's/a/b/' f",
        "readlink -f x",
        "stat -c %s f",
        "grep -oP 'x' f",
        "xargs -r rm",
        "mktemp --tmpdir x",
        "mapfile -t A < f",
        'n="$(date +%s%N)"',
    ],
)
def test_portability_guard_catches(line: str) -> None:
    assert any(re.search(p, line) for p in _GNU_ISMS), line


@pytest.mark.parametrize(
    "line",
    [
        "STAMP=$(date -u +%F)",
        "cp tenants.caddy.example tenants.caddy",
        'aws s3 cp - "${PREFIX}/x.dump"',
        "sed -nE 's/a/b/p' f",
        "grep -Eq '^x$' f",
        "work=$(mktemp -d)",
        "pg_dump -U postgres -Fc -d feohledger",
    ],
)
def test_portability_guard_allows(line: str) -> None:
    assert not any(re.search(p, line) for p in _GNU_ISMS), line


# The checks the VM cannot influence (decisions §490): a compromised VM could
# list a live tenant as owed, so the job checks the bucket itself first.


def test_erasure_refuses_everything_when_backups_are_stale(tmp_path: Path) -> None:
    """(a) No recent night means a live tenant cannot be told from a removed one."""
    d = _erasure(tmp_path, _queue((ORG, "acme", None)))
    d.rule("aws", "--delimiter", 0, _nights(9, 4, 3))

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert "older than 2 days" in result.stderr
    assert not d.called("delete-object")
    assert not d.called("mark-cleared")


def test_erasure_refuses_everything_without_a_nightly_prefix(tmp_path: Path) -> None:
    d = _erasure(tmp_path, _queue((ORG, "acme", None)))
    d.rule("aws", "--delimiter", 0, "None\\n")

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert not d.called("delete-object")


def test_erasure_fails_red_on_a_tenant_dumped_after_its_removal(tmp_path: Path) -> None:
    """(b) backup.sh dumps every feoh_* database nightly; a dump dated after
    the closure means the database was there to dump — a forged entry naming a
    live tenant, or a removal that failed. Never erased, never marked."""
    d = _erasure(tmp_path, _queue((ORG, "acme", None), age=30))
    d.rule(
        "aws",
        _dates("feoh_acme"),
        0,
        f"pg/{_night(31)}/feoh_acme.dump\\tpg/{_night(0)}/feoh_acme.dump\\n",
    )

    result = _run_erasure(d, "erase")

    assert result.returncode != 0
    assert "AFTER its recorded removal" in result.stderr
    assert not d.called("delete-object")
    assert not d.called("mark-cleared")
    assert not _not_masked(result, ORG, "acme", "feoh_acme")


def test_erasure_defers_a_tenant_still_in_the_newest_nights(tmp_path: Path) -> None:
    """(b) A dump on or before the closure, but in one of the newest three
    nights: a restore window. Deferred — not a failure, not marked."""
    d = _erasure(tmp_path, _queue((ORG, "acme", None), age=8))
    d.rule(
        "aws",
        _dates("feoh_acme"),
        0,
        f"pg/{_night(9)}/feoh_acme.dump\\tpg/{_night(2)}/feoh_acme.dump\\n",
    )
    # Freshness: nights 10, 2, 1, 0 — and a closure dated 8 days ago, so the
    # night 2 days ago would be "after" it. Make the entry's closure match it.
    d.queue = _queue((ORG, "acme", None), age=8).replace(_night(8), _night(2))  # type: ignore[attr-defined]

    result = _run_erasure(d, "erase")

    assert result.returncode == 0, result.stderr
    assert "deferred" in result.stdout and "Deferred: 1" in result.stdout
    assert not d.called("delete-object")
    assert not d.called("mark-cleared")


def test_erasure_defers_a_removal_inside_the_cool_off(tmp_path: Path) -> None:
    """(c) A week's cool-off for a mistaken removal."""
    d = _erasure(tmp_path, _queue((ORG, "acme", None), age=3))

    result = _run_erasure(d, "erase")

    assert result.returncode == 0, result.stderr
    assert "deferred until 7" in result.stdout
    assert not d.called("delete-object")
    assert not d.called("mark-cleared")


def test_erasure_erases_once_out_of_the_recent_nights(tmp_path: Path) -> None:
    """Dumps only from before the newest three nights, and older than the
    cool-off: erased."""
    d = _erasure(tmp_path, _queue((ORG, "acme", None), age=10))
    d.rule("aws", _dates("feoh_acme"), 0, f"pg/{_night(10)}/feoh_acme.dump\\n")
    d.rule("aws", _listing("feoh_acme"), 0, f"pg/{_night(10)}/feoh_acme.dump\\tv1\\n", once=True)

    result = _run_erasure(d, "erase")

    assert result.returncode == 0, result.stderr
    assert d.called(f"--key pg/{_night(10)}/feoh_acme.dump --version-id v1")
    assert d.called(f"OrganizationId={ORG},RunId=987")


def test_the_cool_off_matches_the_backend(tmp_path: Path) -> None:
    """The daily check counts an entry as ready only past
    `workspace_closure.ERASURE_COOL_OFF`; the erase job defers by the same
    number, or it would ask for approvals it then refuses."""
    from app.services.workspace_closure import ERASURE_COOL_OFF

    script = (DEPLOY / "backup-erasure.sh").read_text()
    assert f"COOL_OFF_DAYS={ERASURE_COOL_OFF.days}\n" in script


def test_erasure_says_when_the_queue_is_longer_than_one_batch(tmp_path: Path) -> None:
    d = _erasure(tmp_path, _queue((ORG, "acme", None), total=73))
    d.rule("aws", _listing("feoh_acme"), 0, "", once=True)

    result = _run_erasure(d, "erase")

    assert result.returncode == 0, result.stderr
    assert "73 workspace(s) are owed; this run takes the oldest 1" in result.stdout
