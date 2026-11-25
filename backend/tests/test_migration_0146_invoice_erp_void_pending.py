"""Migration 0146 runs against a real tenant: it adds the nullable
``invoices.erp_void_pending`` jsonb marker (decisions §545), backfills nothing,
a re-run is harmless, it reverses, and the control plane is untouched.

The realdb harness builds each tenant with ``create_all``, so the column
already exists there; the test drops it first to stand at 0145 and always
leaves it upgraded so later tests see the current schema. Whole-schema
equality with the model is ``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0146_invoice_erp_void_pending.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0146", REVISION)
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


async def _column(engine) -> tuple | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'invoices' "
                    "AND column_name = 'erp_void_pending'"
                )
            )
        ).first()


@pytest.mark.asyncio
async def test_0146_adds_the_marker_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    assert mig.revision == "0146_invoice_erp_void_pending"
    assert len(mig.revision) <= 32
    assert mig.down_revision == "0145_credit_memo_discount_offer"
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)  # stand at 0145
        assert await _column(engine) is None
        await _run(engine, mig.downgrade)  # idempotent down
        await _run(engine, mig.upgrade)
        assert tuple(await _column(engine)) == ("jsonb", "YES")
        async with engine.connect() as conn:
            marked = (
                await conn.execute(
                    text("SELECT count(*) FROM invoices WHERE erp_void_pending IS NOT NULL")
                )
            ).scalar()
        assert marked == 0  # nothing backfilled
        await _run(engine, mig.upgrade)  # idempotent
        assert tuple(await _column(engine)) == ("jsonb", "YES")
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0146_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        async with engine.connect() as conn:
            exists = (
                await conn.execute(text("SELECT to_regclass('public.invoices') IS NOT NULL"))
            ).scalar()
        assert not exists
    finally:
        await engine.dispose()
