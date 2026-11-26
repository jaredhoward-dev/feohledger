"""Migration 0149 runs against a real tenant: it adds
``expenses.preapproval_id`` (FK + index) and ``expenses.preapproval_drawn_amount``
(decisions §563), backfills nothing, a re-run is harmless, it reverses, and the
control plane is untouched.

The realdb harness builds each tenant with ``create_all``, so the columns
already exist there; the test drops them to stand at 0148 and always leaves
them upgraded so later tests see the current schema. Whole-schema equality with
the model is ``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0149_expense_preapproval_draw.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0149", REVISION)
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


async def _columns(engine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, data_type, numeric_precision, numeric_scale "
                    "FROM information_schema.columns WHERE table_name = 'expenses' "
                    "AND column_name IN ('preapproval_id', 'preapproval_drawn_amount')"
                )
            )
        ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@pytest.mark.asyncio
async def test_0149_adds_the_draw_columns_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    assert mig.revision == "0149_expense_preapproval_draw"
    assert len(mig.revision) <= 32
    assert mig.down_revision == "0148_card_transaction_events"

    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)  # stand at 0148
        assert await _columns(engine) == {}
        await _run(engine, mig.downgrade)  # idempotent down
        await _run(engine, mig.upgrade)
        cols = await _columns(engine)
        assert cols["preapproval_id"][0] == "uuid"
        assert cols["preapproval_drawn_amount"] == ("numeric", 15, 2)
        await _run(engine, mig.upgrade)  # idempotent
        async with engine.connect() as conn:
            fk = (
                await conn.execute(
                    text(
                        "SELECT confrelid::regclass::text FROM pg_constraint "
                        "WHERE conname = 'expenses_preapproval_id_fkey'"
                    )
                )
            ).scalar()
            index = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE tablename = 'expenses' "
                        "AND indexname = 'ix_expenses_preapproval_id'"
                    )
                )
            ).scalar()
            linked = (
                await conn.execute(
                    text("SELECT count(*) FROM expenses WHERE preapproval_id IS NOT NULL")
                )
            ).scalar()
        assert fk == "expense_preapprovals"
        assert "(preapproval_id)" in index
        assert linked == 0  # nothing is backfilled (§141)
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0149_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        assert await _columns(engine) == {}
    finally:
        await engine.dispose()
