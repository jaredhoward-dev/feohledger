"""`POST /api/invoices/{id}/assign` resolves the reviewer inside the caller's org.

`users` is a CONTROL-PLANE table, so the reviewer lookup is the one place on
this route where a bare ``WHERE id = :id`` reaches every tenant's accounts. It
was bare: an admin / ap_manager of tenant A could stamp tenant B's user onto
``Invoice.assigned_to_id``, and ``review.assign_reviewer`` then dispatched the
``invoice_assigned`` notification to them — an email carrying tenant A's invoice
number, vendor name and amount, delivered to somebody in another tenant. The
invoice was left owned by an account that can never act on it, so it also sat in
the queue owned by nobody.

Two sibling routes already got this right and are the shape copied here:
``POST /api/exceptions/{id}/assign`` ("the user must belong to the same
organization") and ``POST /api/auth/delegation`` (same-org **and** active — the
deactivated-delegate guard exists for exactly this quiet-failure reason).
``GET /api/invoices/assignable-reviewers``, this endpoint's own picker, only
ever offers active same-org users, so the API now accepts precisely what the UI
offers.

Also pins that the route honours out-of-office delegation, and that routing
re-checks the delegate — eligibility today and against this invoice's approval
gates — before following it (decisions §511).

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.user import User

pytestmark = pytest.mark.asyncio

TENANT = "a"


async def _seed_invoice(mk, org_id) -> uuid.UUID:
    """One `ready_for_review` invoice — the only status `/assign` accepts."""
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=f"ASSIGN-{uuid.uuid4().hex[:8]}",
                vendor_name="Scope Test Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.ready_for_review,
            )
        )
        await s.commit()
    return inv_id


async def _add_user(realdb, *, org_id, full_name: str, is_active: bool) -> uuid.UUID:
    from app.utils.passwords import pwd_context

    uid = uuid.uuid4()
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        s.add(
            User(
                id=uid,
                email=f"{uuid.uuid4().hex[:10]}@assign-scope.test",
                full_name=full_name,
                hashed_password=pwd_context.hash("Passw0rd!xyz"),
                is_active=is_active,
                organization_id=org_id,
                must_change_password=False,
            )
        )
        await s.commit()
    return uid


async def _assigned_to(mk, inv_id):
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        return inv.assigned_to_id, inv.assigned_to


async def test_cannot_assign_to_another_tenants_user(realdb):
    """The cross-tenant hole: tenant B's user must not become tenant A's reviewer."""
    info_a = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info_a.org_id)
    foreign_user_id = realdb.info("b").users["ap_manager"]

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            f"/api/invoices/{inv_id}/assign", json={"user_id": str(foreign_user_id)}
        )

    assert resp.status_code == 404, resp.text
    assigned_id, assigned_name = await _assigned_to(mk, inv_id)
    assert assigned_id is None, "a foreign tenant's user was stamped onto the invoice"
    assert assigned_name is None


async def test_cannot_assign_to_a_deactivated_user(realdb):
    """Same quiet failure `POST /api/auth/delegation` already refuses: the
    invoice would be owned by an account that can never sign in."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id)
    retired = await _add_user(
        realdb, org_id=info.org_id, full_name="Retired Reviewer", is_active=False
    )

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/assign", json={"user_id": str(retired)})

    assert resp.status_code == 404, resp.text
    assigned_id, _ = await _assigned_to(mk, inv_id)
    assert assigned_id is None


async def test_an_active_same_org_reviewer_still_assigns(realdb):
    """Surgical: the guard narrows the lookup, it doesn't break the happy path."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id)
    reviewer_id = info.users["ap_manager"]

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/assign", json={"user_id": str(reviewer_id)})

    assert resp.status_code == 200, resp.text
    assigned_id, assigned_name = await _assigned_to(mk, inv_id)
    assert assigned_id == reviewer_id
    assert assigned_name


async def test_a_malformed_user_id_is_a_validation_error(realdb):
    """`AssignReviewerRequest.user_id` is a bare `str`, so the UUID parse used to
    raise out of the handler — a 500 on attacker-controlled input."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = await _seed_invoice(mk, info.org_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/assign", json={"user_id": "not-a-uuid"})

    assert resp.status_code == 422, resp.text


async def test_notification_recipients_are_org_scoped(realdb):
    """Defense in depth at the dispatch layer.

    `notify_event`'s recipient loop documents "Recipient no longer exists /
    wrong org — skip silently", but `_load_recipients` never filtered by org, so
    the wrong-org half of that comment was not true. Any caller that fails to
    scope its own lookup must not be able to address another tenant's inbox.
    """
    from app.database import dispatch_engine_scope
    from app.services.notification_dispatch import _load_recipients

    info_a = realdb.info(TENANT)
    own = info_a.users["ap_manager"]
    foreign = realdb.info("b").users["ap_manager"]

    ctrl_mk = realdb.control_sessionmaker()
    async with dispatch_engine_scope(control_sessionmaker=ctrl_mk):
        scoped = await _load_recipients([own, foreign], info_a.org_id)
        unscoped = await _load_recipients([own, foreign])

    assert set(scoped) == {own}, "a foreign-org recipient resolved for this tenant's dispatch"
    # The unscoped call is what every non-`notify_event` caller would get; both
    # ids exist, so the scoped result above is a real filter, not an empty DB.
    assert set(unscoped) == {own, foreign}


# ---------------------------------------------------------------------------
# Out-of-office delegation on the manual assign (decisions §511)
#
# `/assign` used to call `review.assign_reviewer` without `control_db`, so the
# delegation branch never ran — while the docs and the Profile → Out of office
# panel told the delegator new assignments would go to their delegate. And the
# routing followed any stored delegation without re-checking the delegate.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def ooo_users(realdb):
    """A factory for control-plane users with a system role in tenant A's org,
    removed on teardown (control-plane rows persist across tests)."""
    from sqlalchemy import delete

    from app.models.user import Role, UserRole

    mk = realdb.control_sessionmaker()
    org_id = realdb.info(TENANT).org_id
    created: list[uuid.UUID] = []

    async def _make(*, role: str = "ap_manager", delegate_to: uuid.UUID | None = None):
        uid = uuid.uuid4()
        async with mk() as s:
            s.add(
                User(
                    id=uid,
                    email=f"ooo-{uid}@assign-scope.test",
                    full_name=f"OOO {role} {uid.hex[:6]}",
                    hashed_password=None,
                    is_active=True,
                    organization_id=org_id,
                    must_change_password=False,
                    delegate_to_id=delegate_to,
                    delegate_until=(datetime.now(UTC) + timedelta(days=2)) if delegate_to else None,
                )
            )
            await s.flush()
            system_role = (
                (
                    await s.execute(
                        select(Role).where(Role.name == role, Role.organization_id.is_(None))
                    )
                )
                .scalars()
                .first()
            )
            assert system_role is not None, role
            s.add(UserRole(user_id=uid, role_id=system_role.id))
            await s.commit()
        created.append(uid)
        return uid

    yield _make

    async with mk() as s:
        await s.execute(delete(UserRole).where(UserRole.user_id.in_(created)))
        await s.execute(delete(User).where(User.id.in_(created)))
        await s.commit()


async def _assignment_audit(mk, inv_id):
    from app.models.workflow import AuditLog

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == inv_id,
                        AuditLog.action == "invoice.assigned_for_review",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    return rows[0].details


async def test_manual_assign_follows_an_active_delegation(realdb, ooo_users):
    """A → B active: assigning to A lands on B, and the audit row names A."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    delegate = await ooo_users()
    delegator = await ooo_users(delegate_to=delegate)
    inv_id = await _seed_invoice(mk, info.org_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/assign", json={"user_id": str(delegator)})

    assert resp.status_code == 200, resp.text
    assigned_id, _ = await _assigned_to(mk, inv_id)
    assert assigned_id == delegate
    details = await _assignment_audit(mk, inv_id)
    assert details["reviewer_id"] == str(delegate)
    assert details["delegated_from"] == str(delegator)


async def test_manual_assign_keeps_the_original_when_the_delegate_cannot_approve(realdb, ooo_users):
    """A delegation naming someone without `invoice.approve` — saved before
    §511 checked it, or the delegate's role edited since — is not followed:
    the invoice would sit with someone who can never act on it."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    clerk = await ooo_users(role="ap_clerk")
    delegator = await ooo_users(delegate_to=clerk)
    inv_id = await _seed_invoice(mk, info.org_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/assign", json={"user_id": str(delegator)})

    assert resp.status_code == 200, resp.text
    assigned_id, _ = await _assigned_to(mk, inv_id)
    assert assigned_id == delegator
    assert "delegated_from" not in await _assignment_audit(mk, inv_id)


async def test_manual_assign_keeps_the_original_when_the_delegate_uploaded_the_invoice(
    realdb, ooo_users
):
    """Per-invoice: the delegate is in THIS invoice's segregation set (they
    uploaded it), so the approval gate would refuse them — routing reads that
    gate and leaves the invoice with the original reviewer."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    delegate = await ooo_users()
    delegator = await ooo_users(delegate_to=delegate)
    inv_id = await _seed_invoice(mk, info.org_id)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        inv.uploaded_by_id = delegate
        await s.commit()

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/assign", json={"user_id": str(delegator)})

    assert resp.status_code == 200, resp.text
    assigned_id, _ = await _assigned_to(mk, inv_id)
    assert assigned_id == delegator
    assert "delegated_from" not in await _assignment_audit(mk, inv_id)


# ---------------------------------------------------------------------------
# DB-free: `assign_reviewer` follows a delegation only past the approval gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "lands_on_delegate"),
    [
        pytest.param(None, True, id="delegate-can-approve"),
        pytest.param(
            {"code": "approval_segregation", "message": "", "params": {}},
            False,
            id="delegate-in-segregation-set",
        ),
    ],
)
async def test_assign_reviewer_reads_the_approval_gates_for_the_delegate(
    monkeypatch, verdict, lands_on_delegate
):
    """`review.delegate_refusal` runs `review_refusals` FOR THE DELEGATE; a
    refusal keeps the original reviewer and drops `delegated_from`."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import review

    original = uuid.uuid4()
    delegate = SimpleNamespace(id=uuid.uuid4(), full_name="Stand In", roles=[])
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        invoice_number="X-1",
        vendor_name="V",
        amount=Decimal("1.00"),
        currency="USD",
        assigned_to_id=None,
        assigned_to=None,
    )
    seen_actor = []

    async def _refusals(db, inv, *, actor, org_settings):
        seen_actor.append(actor)
        return {"approve": verdict, "reject": None}

    audits = []

    async def _audit(db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(
        "app.services.delegation.eligible_delegate_of", AsyncMock(return_value=delegate)
    )
    monkeypatch.setattr(review, "review_refusals", _refusals)
    monkeypatch.setattr(review, "get_workflow_instance", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.audit_dispatch.dispatch_audit", _audit)
    monkeypatch.setattr(
        "app.services.notification_dispatch.notify_event", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(review, "approval_message_facts", AsyncMock(return_value=None))

    await review.assign_reviewer(
        AsyncMock(),
        invoice,
        actor_id=uuid.uuid4(),
        reviewer_id=original,
        reviewer_name="Original",
        control_db=AsyncMock(),
        org_settings={},
    )

    assert seen_actor == [delegate]
    expected = delegate.id if lands_on_delegate else original
    assert invoice.assigned_to_id == expected
    details = audits[0]["details"]
    assert details["reviewer_id"] == str(expected)
    if lands_on_delegate:
        assert details["delegated_from"] == str(original)
    else:
        assert "delegated_from" not in details
