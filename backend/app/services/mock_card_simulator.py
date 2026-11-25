"""Charge a mock virtual card on a laptop: the local equivalent of the issuer.

Since decisions §537 a payment run's card leg counts only what its card was
charged, and the charge arrives as the provider's webhook. The mock card
adapter has no provider behind it, so without this a card-paid invoice on a
laptop stayed at ``payment_scheduled`` for good (guard rail 7, decisions §541).

This sends the event a provider would: an authorization (and, with ``settle``,
its settlement) for one mock card, signed with the tenant's
``webhook_signing_secret`` (or, when it set none, the public
``MOCK_WEBHOOK_LOCAL_SECRET``), POSTed to ``/api/cards/webhook/mock`` on the
ASGI app given. So the real handler verifies it, dedupes it, writes the charge
through ``record_card_spend`` and re-dispatches the run's payment sync, exactly
as for a Lithic delivery. Nothing here writes a card or a payment itself.

Refused (``MockCardSimulationRefused``, by code) in a deployed environment, for
a tenant whose card provider is not ``mock``, and for a card that is not a mock
card. ``scripts/simulate_card_charge.py`` (``pnpm card:simulate``) is the CLI.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.organization import Organization
from app.models.virtual_card import VirtualCard
from app.services.card_adapters.mock_adapter import (
    MOCK_EVENT_AUTHORIZATION,
    MOCK_EVENT_SETTLEMENT,
    MOCK_WEBHOOK_LOCAL_SECRET,
    MOCK_WEBHOOK_PROVIDER,
    mock_card_event,
    sign_mock_card_event,
)

REFUSED_DEPLOYED = "deployed_environment"
REFUSED_NO_TENANT = "no_such_tenant"
REFUSED_PROVIDER_NOT_MOCK = "card_provider_not_mock"
REFUSED_NO_CARD = "no_such_card"
REFUSED_CARD_NOT_MOCK = "card_not_mock"
REFUSED_BAD_AMOUNT = "invalid_amount"

WEBHOOK_PATH = f"/api/cards/webhook/{MOCK_WEBHOOK_PROVIDER}"


class MockCardSimulationRefused(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SimulatedCharge:
    card_id: uuid.UUID
    events: tuple[str, ...]
    card_status: str
    amount_charged: Decimal | None


def _org_card_provider(org: Organization) -> str | None:
    """The provider this tenant's cards are issued through now, resolved the
    way issuance resolves it; None when cards are off or misconfigured."""
    from app.services.card_adapters import UnknownCardProviderError, get_card_adapter
    from app.services.card_issuance import _resolve_card_config

    config = _resolve_card_config(org.settings or {}, settings)
    if config is None:
        return None
    try:
        return get_card_adapter(config).provider_name
    except UnknownCardProviderError:
        return None


async def simulate_mock_card_charge(
    asgi_app,
    *,
    slug: str,
    card_ref: str,
    amount: Decimal | None = None,
    settle: bool = False,
) -> SimulatedCharge:
    """Charge (and with ``settle``, settle) the mock card ``card_ref`` (its id
    or provider token) of tenant ``slug`` through the real card webhook.
    ``amount`` defaults to the card's limit."""
    if settings.is_deployed:
        raise MockCardSimulationRefused(REFUSED_DEPLOYED)
    if amount is not None and (not amount.is_finite() or amount <= 0):
        raise MockCardSimulationRefused(REFUSED_BAD_AMOUNT)
    from app.services.provider_credentials import provider_config

    async with control_session_factory() as ctrl:
        org = (
            await ctrl.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if org is None:
            raise MockCardSimulationRefused(REFUSED_NO_TENANT)
        if _org_card_provider(org) != MOCK_WEBHOOK_PROVIDER:
            raise MockCardSimulationRefused(REFUSED_PROVIDER_NOT_MOCK)
        secret = (await provider_config(org, "cards", db=ctrl) or {}).get(
            "webhook_signing_secret"
        ) or MOCK_WEBHOOK_LOCAL_SECRET

    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    clauses = [VirtualCard.provider_card_id == card_ref]
    try:
        clauses.append(VirtualCard.id == uuid.UUID(card_ref))
    except ValueError:
        pass
    async with factory() as db:
        card = (await db.execute(select(VirtualCard).where(or_(*clauses)))).scalar_one_or_none()
        if card is None:
            raise MockCardSimulationRefused(REFUSED_NO_CARD)
        if card.card_provider != MOCK_WEBHOOK_PROVIDER:
            raise MockCardSimulationRefused(REFUSED_CARD_NOT_MOCK)
        card_id, token = card.id, card.provider_card_id
        figure = amount if amount is not None else card.amount_limit

    events = [MOCK_EVENT_AUTHORIZATION] + ([MOCK_EVENT_SETTLEMENT] if settle else [])
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        for event_type in events:
            body = mock_card_event(token, event_type=event_type, amount=figure)
            await client.post(
                WEBHOOK_PATH,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Webhook-Signature": sign_mock_card_event(secret, body),
                },
            )

    async with factory() as db:
        card = await db.get(VirtualCard, card_id)
        return SimulatedCharge(
            card_id=card_id,
            events=tuple(events),
            card_status=card.status,
            amount_charged=card.amount_charged,
        )
