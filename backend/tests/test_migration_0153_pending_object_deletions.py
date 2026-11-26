"""Migration 0153 runs against a real tenant: it creates
``pending_object_deletions`` (decisions §592), a re-run is harmless, it
reverses, and the control plane is untouched.

The realdb harness builds each tenant with ``create_all``, so the table already
exists there; the test drops it to stand at the previous revision and always
leaves it upgraded so later tests see the current schema. Whole-schema equality
with the model is ``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0153_pending_object_deletions.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0153", REVISION)
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
                text("SELECT to_regclass('public.pending_object_deletions') IS NOT NULL")
            )
        ).scalar()


@pytest.mark.asyncio
async def test_0153_creates_the_table_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    assert mig.revision == "0153_pending_object_deletions"
    assert len(mig.revision) <= 32

    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)
        assert not await _exists(engine)
        await _run(engine, mig.downgrade)  # idempotent down
        await _run(engine, mig.upgrade)
        assert await _exists(engine)
        await _run(engine, mig.upgrade)  # idempotent
        async with engine.connect() as conn:
            indexes = set(
                (
                    await conn.execute(
                        text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE tablename = 'pending_object_deletions'"
                        )
                    )
                ).scalars()
            )
        assert {
            "ix_pending_object_deletions_organization_id",
            "ix_pending_object_deletions_created_at",
        } <= indexes
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0153_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        assert not await _exists(engine)
    finally:
        await engine.dispose()
