"""Migration 0142 runs against a real control plane: a NOT NULL
``requested_by_user_id`` becomes nullable, a re-run is harmless, a downgrade
keeps it nullable, and a tenant database is untouched.

The realdb harness builds its control plane with ``create_all``, so the column
is already nullable there; the test restores the 0119 shape first. Always left
upgraded so later tests see the current schema. Whole-schema equality with the
model is ``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0142_closure_operator_origin.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0142", REVISION)
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


async def _nullable(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'workspace_closures' "
                    "AND column_name = 'requested_by_user_id'"
                )
            )
        ).scalar()


@pytest.mark.asyncio
async def test_0142_lets_an_operator_removal_have_no_requester(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        # The 0119 shape. No row can hold a NULL yet on a control plane at it.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE workspace_closures ALTER COLUMN requested_by_user_id SET NOT NULL"
                )
            )
        assert await _nullable(engine) == "NO"

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _nullable(engine) == "YES"

        await _run(engine, mig.downgrade)  # keeps it: operator rows may hold NULLs
        assert await _nullable(engine) == "YES"
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0142_is_a_no_op_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.upgrade)
        assert await _nullable(engine) is None
    finally:
        await engine.dispose()
