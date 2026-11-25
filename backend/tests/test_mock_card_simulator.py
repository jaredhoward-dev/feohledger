"""The local card charge (decisions §541): a mock card is charged through the
real signed card webhook, so a laptop's card-paid run is not stuck at
``payment_scheduled`` (§537 made the charge the only thing that pays a card
leg). Local only: refused deployed, for a tenant not on the mock provider, and
for a card that is not a mock card.

Runs against the opt-in ``realdb`` fixture.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.virtual_card import CardRebate, VirtualCard
from app.services import mock_card_simulator as sim
from app.services.card_adapters.mock_adapter import (
    MOCK_EVENT_AUTHORIZATION,
    MOCK_WEBHOOK_LOCAL_SECRET,
    mock_card_event,
    sign_mock_card_event,
)
from tests.test_card_money_path_regression import _card_row, _seed_invoice, _seed_run

pytestmark = pytest.mark.asyncio

TENANT = "a"
_TENANT_SECRET = "mock-card-simulator-tenant-secret"


async def _cards(realdb, *, provider: str = "mock", secret: str | None = None) -> None:
    """The tenant's card programme on ``provider``, with ``secret`` sealed as
    its webhook signing secret (None: no secret)."""
    from app.services.provider_credentials import update_secrets

    org_id = realdb.info(TENANT).org_id
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = {
            "enabled": True,
            "program_type": "byok",
            "provider": provider,
            "region": "US",
            "api_key": "test",
        }
        org.settings = {**(org.settings or {}), "cards": cfg}
        if secret is None:
            await update_secrets(s, org_id, "cards", {}, ["webhook_signing_secret"])
        else:
            await update_secrets(s, org_id, "cards", {"webhook_signing_secret": secret}, [])
        await s.commit()


async def _run_card_leg(realdb, *, provider: str = "mock", limit=Decimal("100.00")):
    """A run's card leg as `_execute_single_payment` leaves it: the payment
    `completed`, its card issued and uncharged."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _seed_invoice(mk, org_id, amount=limit)
    _, pay_id = await _seed_run(mk, org_id, inv, amount=limit, creator_id=None)
    async with mk() as s:
        await s.execute(
            Payment.__table__.update().where(Payment.id == pay_id).values(status="completed")
        )
        card = _card_row(
            inv,
            org_id,
            provider_card_id=f"mock_card_{uuid.uuid4().hex[:12]}",
            limit=limit,
            provider=provider,
        )
        card.payment_id = pay_id
        s.add(card)
        await s.commit()
        return card.id, card.provider_card_id, pay_id


def _app():
    from app.main import app

    return app


@pytest.mark.parametrize("secret", [None, _TENANT_SECRET])
async def test_the_simulator_authorizes_a_mock_card_through_the_real_webhook(realdb, secret):
    """With or without a tenant secret, the signed event goes through the
    handler and the card is authorized (`charged`). An authorization is a hold,
    not money moved (decisions §544): nothing is written onto the run payment
    and no payment sync is dispatched, so the leg stays in flight."""
    from app.services.payment_settlement import COVERAGE_IN_FLIGHT
    from app.services.payment_settlement_record import payment_coverage

    await _cards(realdb, secret=secret)
    card_id, token, pay_id = await _run_card_leg(realdb)

    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        result = await sim.simulate_mock_card_charge(
            _app(), slug=realdb.info(TENANT).slug, card_ref=token
        )

    assert result.card_id == card_id and result.events == (MOCK_EVENT_AUTHORIZATION,)
    assert result.card_status == "charged" and result.amount_charged == Decimal("100.00")
    async with realdb.sessionmaker(TENANT)() as s:
        payment = await s.get(Payment, pay_id)
        coverage = await payment_coverage(s, payment, "USD")
    assert payment.settled_amount is None
    assert coverage.state == COVERAGE_IN_FLIGHT
    dispatched.assert_not_awaited()


@pytest.mark.parametrize("secret", [None, _TENANT_SECRET])
async def test_settle_charges_the_run_payment_in_full_and_dispatches_once(realdb, secret):
    """`--settle` is the laptop's path to `paid`: the settled figure lands on
    the run payment, coverage reads `covered`, and the sync is dispatched once
    (by the settlement, not the authorization)."""
    from app.services.payment_settlement import COVERAGE_COVERED
    from app.services.payment_settlement_record import payment_coverage

    await _cards(realdb, secret=secret)
    card_id, token, pay_id = await _run_card_leg(realdb)

    with patch(
        "app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()
    ) as dispatched:
        result = await sim.simulate_mock_card_charge(
            _app(), slug=realdb.info(TENANT).slug, card_ref=token, settle=True
        )

    assert result.card_status == "completed"
    async with realdb.sessionmaker(TENANT)() as s:
        payment = await s.get(Payment, pay_id)
        coverage = await payment_coverage(s, payment, "USD")
    assert (payment.settled_amount, payment.settled_currency) == (Decimal("100.00"), "USD")
    assert coverage.state == COVERAGE_COVERED
    dispatched.assert_awaited_once()


async def test_settle_sends_the_settlement_too_at_the_amount_given(realdb):
    await _cards(realdb)
    card_id, _, pay_id = await _run_card_leg(realdb)

    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        result = await sim.simulate_mock_card_charge(
            _app(),
            slug=realdb.info(TENANT).slug,
            card_ref=str(card_id),
            amount=Decimal("90.00"),
            settle=True,
        )

    assert result.card_status == "completed" and result.amount_charged == Decimal("90.00")
    async with realdb.sessionmaker(TENANT)() as s:
        payment = await s.get(Payment, pay_id)
        rebates = (
            (await s.execute(select(CardRebate).where(CardRebate.virtual_card_id == card_id)))
            .scalars()
            .all()
        )
    assert payment.settled_amount == Decimal("90.00")  # short: the settled-short hold
    assert len(rebates) == 1


async def _post_mock(realdb, body: bytes, secret: str) -> int:
    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            "/api/cards/webhook/mock",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Webhook-Signature": sign_mock_card_event(secret, body),
            },
        )
    return resp.status_code


async def _status(realdb, card_id) -> str:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(VirtualCard, card_id)).status


async def test_the_local_key_is_not_accepted_once_the_tenant_has_its_own(realdb):
    await _cards(realdb, secret=_TENANT_SECRET)
    card_id, token, _ = await _run_card_leg(realdb)
    body = mock_card_event(token, event_type="authorization", amount=Decimal("100.00"))
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert await _post_mock(realdb, body, MOCK_WEBHOOK_LOCAL_SECRET) == 204
    assert await _status(realdb, card_id) == "active"


async def test_a_deployed_environment_refuses_the_simulator_and_the_mock_webhook(
    realdb, monkeypatch
):
    await _cards(realdb)
    card_id, token, _ = await _run_card_leg(realdb)
    monkeypatch.setattr(settings, "environment", "production")

    with pytest.raises(sim.MockCardSimulationRefused) as refused:
        await sim.simulate_mock_card_charge(_app(), slug=realdb.info(TENANT).slug, card_ref=token)
    assert refused.value.code == sim.REFUSED_DEPLOYED

    # The webhook's mock branch is gone there too: a correctly signed event is
    # an unknown provider, answered 204 with nothing changed.
    body = mock_card_event(token, event_type="authorization", amount=Decimal("100.00"))
    with patch("app.services.payment_erp_sync.dispatch_payment_sync", new=AsyncMock()):
        assert await _post_mock(realdb, body, MOCK_WEBHOOK_LOCAL_SECRET) == 204
    assert await _status(realdb, card_id) == "active"


async def test_a_tenant_not_on_the_mock_provider_is_refused(realdb):
    await _cards(realdb, provider="lithic")
    _, token, _ = await _run_card_leg(realdb)
    with pytest.raises(sim.MockCardSimulationRefused) as refused:
        await sim.simulate_mock_card_charge(_app(), slug=realdb.info(TENANT).slug, card_ref=token)
    assert refused.value.code == sim.REFUSED_PROVIDER_NOT_MOCK


async def test_a_card_that_is_not_a_mock_card_is_refused(realdb):
    await _cards(realdb)
    card_id, token, _ = await _run_card_leg(realdb, provider="nium")
    with pytest.raises(sim.MockCardSimulationRefused) as refused:
        await sim.simulate_mock_card_charge(_app(), slug=realdb.info(TENANT).slug, card_ref=token)
    assert refused.value.code == sim.REFUSED_CARD_NOT_MOCK
    assert await _status(realdb, card_id) == "active"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"slug": "no-such-tenant-x", "card_ref": "tok"}, sim.REFUSED_NO_TENANT),
        ({"card_ref": "mock_card_missing"}, sim.REFUSED_NO_CARD),
        ({"card_ref": "tok", "amount": Decimal("0")}, sim.REFUSED_BAD_AMOUNT),
        ({"card_ref": "tok", "amount": Decimal("NaN")}, sim.REFUSED_BAD_AMOUNT),
    ],
)
async def test_unusable_requests_are_refused(realdb, kwargs, code):
    await _cards(realdb)
    kwargs = {"slug": realdb.info(TENANT).slug, **kwargs}
    with pytest.raises(sim.MockCardSimulationRefused) as refused:
        await sim.simulate_mock_card_charge(_app(), **kwargs)
    assert refused.value.code == code


def test_the_event_amount_is_an_exact_decimal_string():
    body = mock_card_event("tok", event_type="authorization", amount=Decimal("0.10"))
    assert b'"amount":"0.10"' in body
