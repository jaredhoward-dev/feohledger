"""`services/card_transaction_memory.py` on its own (decisions §554).

The card webhook tests drive this module end to end; these pin its contract
directly, against the real `card_transaction_events` table, because the
guarantees are the unique index's (`INSERT ... ON CONFLICT DO NOTHING` on
`(virtual_card_id, kind, event_key)`), which no fake session reproduces:

* a lapse that came first is remembered once, found by its transaction id, and
  consumed by exactly ONE authorization — Nium's `authCode` fallback is not
  unique per transaction, so a second authorization carrying the same code must
  not be applied as lapsed;
* a clearing is recorded once per identity, per card.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.virtual_card import CardTransactionEvent
from app.services import card_transaction_memory as memory
from tests.test_card_money_path_regression import TENANT, _card_row, _seed_invoice

pytestmark = pytest.mark.asyncio


async def _card(realdb, *, provider: str = "nium"):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _seed_invoice(mk, org_id, amount=Decimal("100.00"))
    async with mk() as s:
        card = _card_row(
            inv,
            org_id,
            provider_card_id=f"tok_{uuid.uuid4().hex[:12]}",
            limit=Decimal("100.00"),
            provider=provider,
        )
        s.add(card)
        await s.commit()
    return mk, card


async def _rows(s, card) -> list[CardTransactionEvent]:
    return list(
        (
            await s.execute(
                select(CardTransactionEvent).where(CardTransactionEvent.virtual_card_id == card.id)
            )
        )
        .scalars()
        .all()
    )


async def test_a_lapse_is_remembered_once_and_found_by_its_transaction(realdb):
    mk, card = await _card(realdb)
    async with mk() as s:
        assert await memory.remember_lapse(
            s, card, transaction_id="txn_1", lapse="reversal", amount=Decimal("40.00")
        )
        # The same transaction's lapse again (a redelivery past Redis): nothing.
        assert not await memory.remember_lapse(
            s, card, transaction_id="txn_1", lapse="reversal", amount=Decimal("40.00")
        )
        row = await memory.lapse_for(s, card, "txn_1")
        assert row is not None
        assert row.lapse == "reversal" and row.amount == Decimal("40.00")
        assert row.currency == card.currency
        assert await memory.lapse_for(s, card, "txn_other") is None
        assert await memory.lapse_for(s, card, None) is None
        assert await memory.lapse_for(s, card, "") is None
        await s.commit()


async def test_a_lapse_is_consumed_once_and_kept_as_the_record(realdb):
    """Consumed by the one authorization it lapsed: no longer found, but the
    row stays (re-kinded `lapse_applied`) as the record of why that
    authorization left the card unspent."""
    mk, card = await _card(realdb)
    async with mk() as s:
        await memory.remember_lapse(s, card, transaction_id="txn_1", lapse="expiry", amount=None)
        row = await memory.lapse_for(s, card, "txn_1")
        await memory.consume_lapse(s, row)
        assert await memory.lapse_for(s, card, "txn_1") is None
        await s.commit()
    async with mk() as s:
        (kept,) = await _rows(s, card)
    assert kept.kind == memory.KIND_LAPSE_APPLIED
    assert kept.event_key == f"txn_1@{kept.id}"
    assert kept.provider_transaction_id == "txn_1"


async def test_a_reused_nium_auth_code_lapses_only_the_authorization_it_was_applied_to(realdb):
    """Nium's fallback transaction id is `authCode`, which is not unique per
    transaction. A remembered lapse used up by one authorization must NOT lapse
    a later authorization carrying the same code — that one is a real charge.
    A fresh lapse of the reused code is remembered and consumed again, without
    colliding with the first applied row on the per-card unique key."""
    mk, card = await _card(realdb, provider="nium")
    code = "114733"
    async with mk() as s:
        await memory.remember_lapse(s, card, transaction_id=code, lapse="reversal", amount=None)
        # The first authorization with this code finds and consumes the lapse.
        first = await memory.lapse_for(s, card, code)
        assert first is not None
        await memory.consume_lapse(s, first)
        # A second authorization with the same code: no lapse is applied.
        assert await memory.lapse_for(s, card, code) is None

        # The code reused once more, with its own early lapse.
        assert await memory.remember_lapse(
            s, card, transaction_id=code, lapse="reversal", amount=None
        )
        again = await memory.lapse_for(s, card, code)
        assert again is not None and again.id != first.id
        await memory.consume_lapse(s, again)
        assert await memory.lapse_for(s, card, code) is None
        await s.commit()
    async with mk() as s:
        rows = await _rows(s, card)
    assert sorted(r.kind for r in rows) == [memory.KIND_LAPSE_APPLIED] * 2
    assert len({r.event_key for r in rows}) == 2


async def test_a_clearing_is_recorded_once_per_identity(realdb):
    mk, card = await _card(realdb, provider="lithic")
    async with mk() as s:
        assert not await memory.clearing_seen(s, card, "event:c1")
        assert await memory.record_clearing(
            s, card, clearing_key="event:c1", transaction_id="txn_1", amount=Decimal("100.00")
        )
        assert await memory.clearing_seen(s, card, "event:c1")
        # A replay of the same clearing inserts nothing and says so.
        assert not await memory.record_clearing(
            s, card, clearing_key="event:c1", transaction_id="txn_1", amount=Decimal("100.00")
        )
        # Another clearing of the same transaction is its own row.
        assert await memory.record_clearing(
            s, card, clearing_key="event:c2", transaction_id="txn_1", amount=Decimal("50.00")
        )
        await s.commit()
    async with mk() as s:
        rows = await _rows(s, card)
    assert sorted(r.event_key for r in rows) == ["event:c1", "event:c2"]
    assert all(r.kind == memory.KIND_CLEARING and r.lapse is None for r in rows)
    assert {r.amount for r in rows} == {Decimal("100.00"), Decimal("50.00")}


async def test_memory_is_per_card_and_per_kind(realdb):
    """The unique key is `(card, kind, event_key)`: the same key on another
    card, or a clearing and a lapse sharing a key, never collide."""
    mk, card = await _card(realdb, provider="lithic")
    _mk, other = await _card(realdb, provider="lithic")
    async with mk() as s:
        assert await memory.record_clearing(
            s, card, clearing_key="txn_1", transaction_id="txn_1", amount=None
        )
        assert await memory.record_clearing(
            s, other, clearing_key="txn_1", transaction_id="txn_1", amount=None
        )
        assert await memory.remember_lapse(
            s, card, transaction_id="txn_1", lapse="reversal", amount=None
        )
        assert not await memory.clearing_seen(s, card, "txn_2")
        await s.commit()


async def test_an_overlong_key_is_clipped_to_the_column_and_still_deduped(realdb):
    mk, card = await _card(realdb, provider="lithic")
    key = "delivery:" + "x" * 400
    async with mk() as s:
        assert await memory.record_clearing(
            s, card, clearing_key=key, transaction_id="t" * 400, amount=None
        )
        assert await memory.clearing_seen(s, card, key)
        assert not await memory.record_clearing(
            s, card, clearing_key=key, transaction_id="t" * 400, amount=None
        )
        await s.commit()
    async with mk() as s:
        (row,) = await _rows(s, card)
    assert len(row.event_key) == 255 and len(row.provider_transaction_id) == 255
