"""The seed records Terms / Privacy acceptance for every account it creates.

The web app draws a blocking accept step in place of the app shell while
`GET /api/auth/me` says `legal_acceptance_required` (decisions §495, §497).
The seeded demo and per-worker e2e accounts were created by a script, not a
signup or a set-password link, so they hold no acceptance rows; without one
the step would stand between every e2e spec (and every demo login) and the
app. `scripts/seed.py` therefore records the versions in force for every
user of the orgs it seeds, through the service's one writer, on the fresh
path AND the already-seeded path (a control plane seeded before this existed,
or before a version bump, is repaired by re-running the seed).

A source check, like `test_card_provider_local_first.py`'s seed half: the
seed is a script nothing imports, and the behaviour that matters is which
paths call the helper.
"""

import ast
from pathlib import Path

_SEED = Path(__file__).resolve().parents[1] / "scripts" / "seed.py"
_HELPER = "ensure_seeded_users_accepted_legal"


def _functions() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(_SEED.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}


def _calls(fn: ast.AST, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == name)
        )
    ]


def test_helper_records_the_versions_in_force_through_the_service():
    helper = _functions()[_HELPER]
    calls = _calls(helper, "record_acceptances")
    assert len(calls) == 1, "the helper must write through legal_acceptance.record_acceptances"
    kwargs = {k.arg: k.value for k in calls[0].keywords}
    versions = kwargs["versions"]
    assert isinstance(versions, ast.Name) and versions.id == "CURRENT_LEGAL_VERSIONS"
    # Never a control's name: no person accepted anything for a fixture account.
    source = kwargs["source"]
    assert isinstance(source, ast.Name) and source.id == "SEED_LEGAL_SOURCE"


def test_demo_control_plane_records_acceptance_on_both_paths():
    # One call on the fresh path, one on the early-return "already seeded" path.
    assert len(_calls(_functions()["seed_control_plane"], _HELPER)) == 2


def test_e2e_control_plane_records_acceptance_for_every_worker_tenant():
    fn = _functions()["seed_e2e_control_plane"]
    calls = _calls(fn, _HELPER)
    assert len(calls) == 1
    # After the per-tenant loop (whose `continue` skips an existing tenant),
    # over every tenant in `created` — so a pre-existing worker is backfilled.
    assert "created" in ast.unparse(calls[0].args[1])
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert all(calls[0].lineno > loop.end_lineno for loop in loops if _calls(loop, "User"))


# ---------------------------------------------------------------------------
# Supplier-portal users (decisions §499, §500)
# ---------------------------------------------------------------------------

_PORTAL_HELPER = "ensure_seeded_portal_users_acknowledged_privacy"


def test_portal_helper_records_through_the_service_with_the_seed_source():
    helper = _functions()[_PORTAL_HELPER]
    calls = _calls(helper, "record_acknowledgement")
    assert len(calls) == 1, "the helper must write through record_acknowledgement"
    source = {k.arg: k.value for k in calls[0].keywords}["source"]
    assert isinstance(source, ast.Name) and source.id == "SEED_LEGAL_SOURCE"


def test_every_tenant_seed_acknowledges_for_its_portal_users_on_both_paths():
    """Both tenant seeds create a portal user; each calls the helper on the
    fresh path (after the user is added) and on the early "already seeded"
    return, so the portal re-prompt never meets a seeded supplier."""
    functions = _functions()
    for name in ("seed_tenant", "seed_tenant_lean"):
        fn = functions[name]
        calls = _calls(fn, _PORTAL_HELPER)
        assert len(calls) == 2, name
        adds = _calls(fn, "_make_portal_user")
        assert adds, name
        assert max(c.lineno for c in calls) > max(a.lineno for a in adds), name
