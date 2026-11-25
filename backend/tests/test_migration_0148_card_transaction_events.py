"""Migration 0148 runs against a real tenant: it creates
``card_transaction_events`` (decisions §554) with its per-card unique key and
cascade from ``virtual_cards``, a re-run is harmless, it reverses, and the
control plane is untouched.

The realdb harness builds each tenant with ``create_all``, so the table already
exists there; the test drops it to stand at 0147 and always leaves it upgraded
so later tests see the current schema. Whole-schema equality with the model is
``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0148_card_transaction_events.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0148", REVISION)
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


async def _exists(engine) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT to_regclass('public.card_transaction_events') IS NOT NULL")
            )
        ).scalar()


@pytest.mark.asyncio
async def test_0148_creates_the_table_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    assert mig.revision == "0148_card_transaction_events"
    assert len(mig.revision) <= 32
    assert mig.down_revision == "0147_card_first_authorized_at"

    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)  # stand at 0147
        assert not await _exists(engine)
        await _run(engine, mig.downgrade)  # idempotent down
        await _run(engine, mig.upgrade)
        assert await _exists(engine)
        await _run(engine, mig.upgrade)  # idempotent
        async with engine.connect() as conn:
            unique = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE tablename = "
                        "'card_transaction_events' AND indexname = "
                        "'uq_card_transaction_events_card_kind_key'"
                    )
                )
            ).scalar()
            on_delete = (
                await conn.execute(
                    text(
                        "SELECT confdeltype::text FROM pg_constraint WHERE conrelid = "
                        "'card_transaction_events'::regclass AND contype = 'f'"
                    )
                )
            ).scalar()
        assert "UNIQUE" in unique and "(virtual_card_id, kind, event_key)" in unique
        assert on_delete == "c"  # ON DELETE CASCADE: an invoice delete clears it
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0148_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        assert not await _exists(engine)
    finally:
        await engine.dispose()
