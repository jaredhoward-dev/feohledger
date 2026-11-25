"""What the card webhook remembers about a provider transaction (decisions §554).

The card webhook applies each delivery inside one row-locked transaction and
dedupes it by event id in Redis. Two facts must outlive the delivery that
carried them, so they are kept in ``card_transaction_events`` (migration 0148):

* **A lapse that came first.** A reversal or expiry delivered before its
  authorization finds an unspent card and nothing to release. It is recorded
  under the provider's transaction id, and the late authorization of that same
  transaction is applied as already lapsed (:func:`lapse_for`).
* **Each clearing applied.** A settled card can clear again (an incremental
  capture); each clearing is recorded under its own identity, so it is added to
  the card exactly once even if the Redis claim has expired or Redis was down
  (:func:`record_clearing`).

Both writes are ``INSERT ... ON CONFLICT DO NOTHING`` on the per-card unique
``(kind, event_key)``: a replay inserts nothing and reports so. The caller
holds the card row lock, so two deliveries for one card never race here.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.virtual_card import CardTransactionEvent, VirtualCard

KIND_LAPSE = "lapse"
#: A lapse already applied to its authorization (:func:`consume_lapse`).
KIND_LAPSE_APPLIED = "lapse_applied"
KIND_CLEARING = "clearing"

#: Column width of ``event_key`` / ``provider_transaction_id``.
_KEY_MAX = 255


def _clip(value: str | None) -> str | None:
    return None if value is None else str(value)[:_KEY_MAX]


async def _insert(db: AsyncSession, card: VirtualCard, **values) -> bool:
    stmt = (
        pg_insert(CardTransactionEvent)
        .values(
            virtual_card_id=card.id,
            organization_id=card.organization_id,
            **values,
        )
        .on_conflict_do_nothing(index_elements=["virtual_card_id", "kind", "event_key"])
        .returning(CardTransactionEvent.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def remember_lapse(
    db: AsyncSession,
    card: VirtualCard,
    *,
    transaction_id: str,
    lapse: str,
    amount: Decimal | None,
) -> bool:
    """Record a reversal / expiry that arrived before its authorization.
    Returns False when this transaction's lapse was already recorded."""
    return await _insert(
        db,
        card,
        id=uuid.uuid4(),
        kind=KIND_LAPSE,
        event_key=_clip(transaction_id),
        provider_transaction_id=_clip(transaction_id),
        lapse=lapse,
        amount=amount,
        currency=card.currency,
    )


async def lapse_for(
    db: AsyncSession, card: VirtualCard, transaction_id: str | None
) -> CardTransactionEvent | None:
    """The lapse recorded for ``transaction_id`` on ``card``, if one came first."""
    if not transaction_id:
        return None
    return (
        await db.execute(
            select(CardTransactionEvent).where(
                CardTransactionEvent.virtual_card_id == card.id,
                CardTransactionEvent.kind == KIND_LAPSE,
                CardTransactionEvent.event_key == _clip(transaction_id),
            )
        )
    ).scalar_one_or_none()


async def consume_lapse(db: AsyncSession, row: CardTransactionEvent) -> None:
    """Mark a remembered lapse as applied, so it matches the one authorization
    it lapsed and never a later one carrying the same transaction id (a Nium
    auth code is not unique per transaction). Kept, not deleted: the row is the
    record of why that authorization left the card unspent."""
    row.kind = KIND_LAPSE_APPLIED
    # Suffixed with the row's own id, so a second applied lapse of a reused id
    # never collides with this one on the per-card unique key.
    row.event_key = f"{row.event_key}@{row.id}"[:_KEY_MAX]
    await db.flush()


async def clearing_seen(db: AsyncSession, card: VirtualCard, clearing_key: str) -> bool:
    """Whether the clearing ``clearing_key`` was already applied to ``card``."""
    return (
        await db.execute(
            select(CardTransactionEvent.id).where(
                CardTransactionEvent.virtual_card_id == card.id,
                CardTransactionEvent.kind == KIND_CLEARING,
                CardTransactionEvent.event_key == _clip(clearing_key),
            )
        )
    ).scalar_one_or_none() is not None


async def record_clearing(
    db: AsyncSession,
    card: VirtualCard,
    *,
    clearing_key: str,
    transaction_id: str | None,
    amount: Decimal | None,
) -> bool:
    """Record one clearing on ``card``. Returns False on a replay."""
    return await _insert(
        db,
        card,
        id=uuid.uuid4(),
        kind=KIND_CLEARING,
        event_key=_clip(clearing_key),
        provider_transaction_id=_clip(transaction_id),
        lapse=None,
        amount=amount,
        currency=card.currency,
    )
