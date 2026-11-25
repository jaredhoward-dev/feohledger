"""The server-side Terms / Privacy gate (`docs/decisions.md` §505).

Pins: `get_current_user` refuses an employee who owes an acceptance with a 403
coded `legal_acceptance_required`, reading the count the resolver folded into
its own query; `get_current_vendor_user` does the same for a supplier who owes
the Privacy Policy acknowledgement (`privacy_acknowledgement_required`); the
routes that skip the gate are exactly the listed exemptions; the `/api/v1`
API-key routes never reach either resolver; and, against a real database, an
unaccepted employee is refused on an ordinary route, reaches the exempt ones,
and is let through once they accept.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import delete

from app.api.deps import (
    LEGAL_GATE_EXEMPT_ROUTES,
    get_api_key_principal,
    get_authenticated_user,
    get_current_user,
)
from app.api.portal_deps import (
    PORTAL_LEGAL_GATE_EXEMPT_ROUTES,
    get_authenticated_vendor_user,
    get_current_vendor_user,
)
from app.main import app
from app.models.legal_acceptance import LegalAcceptance
from app.services.legal_acceptance import CURRENT_LEGAL_VERSIONS
from tests.set_password_mail import ACCEPT_CURRENT

pytestmark = pytest.mark.legal_unaccepted


def _calls(route: APIRoute) -> set:
    seen: set = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            seen.add(dep.call)
        stack.extend(dep.dependencies)
    return seen


def _routes():
    """(method, path, route) for every `/api` APIRoute, nested routers included
    (the flattening `test_rbac._iter_api_routes` documents)."""
    from fastapi.routing import iter_route_contexts

    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.route, APIRoute) or not ctx.path.startswith("/api"):
            continue
        for method in sorted(ctx.methods or ()):
            if method != "HEAD":
                yield method, ctx.path, ctx.route


def _ungated(resolver, gate) -> set[tuple[str, str]]:
    return {
        (method, path)
        for method, path, route in _routes()
        if resolver in _calls(route) and gate not in _calls(route)
    }


# ---------------------------------------------------------------------------
# Which routes skip the gate
# ---------------------------------------------------------------------------


def test_only_the_listed_employee_routes_skip_the_gate():
    assert _ungated(get_authenticated_user, get_current_user) == LEGAL_GATE_EXEMPT_ROUTES


def test_only_the_listed_portal_routes_skip_the_gate():
    assert (
        _ungated(get_authenticated_vendor_user, get_current_vendor_user)
        == PORTAL_LEGAL_GATE_EXEMPT_ROUTES
    )


def test_the_api_key_routes_never_reach_a_person_resolver():
    """`/api/v1` is for machine callers: a key is not a person and has no one
    to accept, so it is out of the gate's scope by construction."""
    v1 = [(p, r) for _m, p, r in _routes() if get_api_key_principal in _calls(r)]
    assert v1, "no API-key routes found — the selection has gone blind"
    people = {get_authenticated_user, get_authenticated_vendor_user}
    assert not [p for p, r in v1 if _calls(r) & people]


# ---------------------------------------------------------------------------
# The gate itself (no database)
# ---------------------------------------------------------------------------


def _user(accepted: int | None):
    return SimpleNamespace(id=uuid.uuid4(), legal_current_acceptances=accepted)


@pytest.mark.asyncio
async def test_an_employee_who_owes_an_acceptance_is_refused_with_the_code():
    with pytest.raises(HTTPException) as exc:
        await get_current_user(user=_user(len(CURRENT_LEGAL_VERSIONS) - 1), db=AsyncMock())
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "legal_acceptance_required"


@pytest.mark.asyncio
async def test_an_employee_who_accepted_every_document_passes_without_a_query():
    db = AsyncMock()
    user = _user(len(CURRENT_LEGAL_VERSIONS))
    assert await get_current_user(user=user, db=db) is user
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_a_user_loaded_without_the_count_is_asked_of_the_database():
    user = _user(None)
    with patch(
        "app.services.legal_acceptance.missing_documents", AsyncMock(return_value=["terms"])
    ) as missing:
        with pytest.raises(HTTPException):
            await get_current_user(user=user, db=AsyncMock())
    missing.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_supplier_who_owes_the_acknowledgement_is_refused_with_the_portal_code():
    vu = SimpleNamespace(id=uuid.uuid4())
    with patch(
        "app.services.portal_legal_acceptance.missing_documents",
        AsyncMock(return_value=["privacy"]),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_vendor_user(vu=vu, db=AsyncMock())
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "privacy_acknowledgement_required"
    with patch(
        "app.services.portal_legal_acceptance.missing_documents", AsyncMock(return_value=[])
    ):
        assert await get_current_vendor_user(vu=vu, db=AsyncMock()) is vu


# ---------------------------------------------------------------------------
# End to end (real database)
# ---------------------------------------------------------------------------


async def _drop_rows(realdb, user_id: uuid.UUID) -> None:
    async with realdb.control_sessionmaker()() as s:
        await s.execute(delete(LegalAcceptance).where(LegalAcceptance.user_id == user_id))
        await s.commit()


async def test_an_unaccepted_employee_reaches_only_the_accept_step(realdb):
    admin_id = realdb.info("a").users["admin"]
    await _drop_rows(realdb, admin_id)
    try:
        async with realdb.client(key="a", role="admin") as c:
            refused = await c.get("/api/invoices")
            assert refused.status_code == 403, refused.text
            assert refused.json()["detail"]["code"] == "legal_acceptance_required"
            assert (await c.get("/api/auth/sessions")).status_code == 403

            me = await c.get("/api/auth/me")
            assert me.status_code == 200
            assert me.json()["legal_acceptance_required"] is True

            ok = await c.post("/api/auth/legal-acceptance", json=ACCEPT_CURRENT)
            assert ok.status_code == 200, ok.text
            assert (await c.get("/api/invoices")).status_code == 200
    finally:
        await _drop_rows(realdb, admin_id)


async def test_a_version_bump_gates_an_employee_who_had_accepted(realdb, monkeypatch):
    async with realdb.client(key="a", role="admin") as c:
        assert (await c.get("/api/invoices")).status_code == 200
        monkeypatch.setitem(CURRENT_LEGAL_VERSIONS, "terms", "2099-01-01")
        refused = await c.get("/api/invoices")
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "legal_acceptance_required"
