"""Migration 0144 runs against a real tenant: it creates
``vendor_user_legal_acceptances`` with its unique constraint and no foreign
key, a re-run is harmless (``IF NOT EXISTS``), and the control plane is
untouched.

The realdb harness builds each tenant with ``create_all``, so the table already
exists there; the test drops it first to stand at 0143 and always leaves it
upgraded so later tests see the current schema. Whole-schema equality with the
model is ``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0144_vendor_user_legal_accept.py"
TABLE = "vendor_user_legal_acceptances"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0144", REVISION)
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


async def _table_exists(engine) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT to_regclass('public.{TABLE}') IS NOT NULL"))
        ).scalar()


async def _unique_constraint_columns(engine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                "WHERE c.conname = 'uq_vendor_user_legal_acceptances_doc_version' "
                "AND c.contype = 'u' ORDER BY k.ord"
            )
        )
        return [r[0] for r in rows]


async def _foreign_keys(engine) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint c JOIN pg_class t "
                    f"ON t.oid = c.conrelid WHERE t.relname = '{TABLE}' AND c.contype = 'f'"
                )
            )
        ).scalar()


@pytest.mark.asyncio
async def test_0144_creates_the_table_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    assert mig.down_revision == "0143_legal_acceptances"
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        assert not await _table_exists(engine)

        await _run(engine, mig.upgrade)
        assert await _table_exists(engine)
        assert await _unique_constraint_columns(engine) == ["vendor_user_id", "document", "version"]
        # No foreign key, by design: the row is kept through a DSAR erasure
        # and a portal-user delete (§499).
        assert await _foreign_keys(engine) == 0

        await _run(engine, mig.upgrade)  # idempotent: IF NOT EXISTS
        assert await _unique_constraint_columns(engine) == ["vendor_user_id", "document", "version"]
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0144_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        assert not await _table_exists(engine)
    finally:
        await engine.dispose()
