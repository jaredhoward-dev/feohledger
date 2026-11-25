"""Migration 0145 runs against a real tenant: it adds
``credit_memos.discount_offer_id`` with its ``ON DELETE SET NULL`` foreign key,
its index and the one-live-memo-per-offer partial unique index, backfills
nothing, a re-run is harmless, and the control plane is untouched.

The realdb harness builds each tenant with ``create_all``, so the column
already exists there; the test drops it first to stand at 0144 and always
leaves it upgraded so later tests see the current schema. Whole-schema
equality with the model is ``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = (
    Path(__file__).resolve().parents[1] / "alembic/versions/0145_credit_memo_discount_offer.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0145", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _column_exists(engine) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'credit_memos' AND column_name = 'discount_offer_id'"
                )
            )
        ).scalar() is not None


async def _fk_delete_rule(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT confdeltype::text FROM pg_constraint "
                    "WHERE conname = 'credit_memos_discount_offer_id_fkey' AND contype = 'f'"
                )
            )
        ).scalar()


async def _index_defs(engine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'credit_memos' "
                "AND indexname IN ('ix_credit_memos_discount_offer_id', "
                "'uq_credit_memos_discount_offer_live')"
            )
        )
        return {name: definition for name, definition in rows}


async def _seed_vendor(realdb) -> uuid.UUID:
    from app.models.vendor import Vendor

    async with realdb.sessionmaker("a")() as s:
        vendor = Vendor(organization_id=realdb.info("a").org_id, name="M0145 Vendor")
        s.add(vendor)
        await s.commit()
        return vendor.id


async def _seed_memo(engine, vendor_id: uuid.UUID) -> uuid.UUID:
    """One pre-0145 memo, written as 0144's schema allows, to prove the
    upgrade infers nothing for it."""
    memo_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO credit_memos (id, memo_number, vendor_id, amount, currency, "
                "status, organization_id, created_at, updated_at) VALUES "
                "(:id, 'CM-0145', :vendor, 20.00, 'USD', 'applied', :org, now(), now())"
            ),
            {"id": memo_id, "vendor": vendor_id, "org": uuid.uuid4()},
        )
    return memo_id


@pytest.mark.asyncio
async def test_0145_adds_the_link_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    assert mig.revision == "0145_credit_memo_discount_offer"
    assert len(mig.revision) <= 32
    assert mig.down_revision == "0144_vendor_user_legal_accept"
    vendor_id = await _seed_vendor(realdb)
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)  # stand at 0144
        assert not await _column_exists(engine)
        assert await _index_defs(engine) == {}
        memo_id = await _seed_memo(engine, vendor_id)

        await _run(engine, mig.upgrade)
        assert await _column_exists(engine)
        assert await _fk_delete_rule(engine) == "n"  # ON DELETE SET NULL
        defs = await _index_defs(engine)
        assert set(defs) == {
            "ix_credit_memos_discount_offer_id",
            "uq_credit_memos_discount_offer_live",
        }
        live = defs["uq_credit_memos_discount_offer_live"]
        assert live.startswith("CREATE UNIQUE INDEX")
        assert "discount_offer_id IS NOT NULL" in live
        assert "'void'" in live

        # No backfill: the pre-0145 memo stays an ordinary credit.
        async with engine.connect() as conn:
            linked = (
                await conn.execute(
                    text("SELECT discount_offer_id FROM credit_memos WHERE id = :id"),
                    {"id": memo_id},
                )
            ).scalar()
        assert linked is None

        await _run(engine, mig.upgrade)  # idempotent
        assert set(await _index_defs(engine)) == set(defs)
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0145_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        async with engine.connect() as conn:
            exists = (
                await conn.execute(text("SELECT to_regclass('public.credit_memos') IS NOT NULL"))
            ).scalar()
        assert not exists
    finally:
        await engine.dispose()
