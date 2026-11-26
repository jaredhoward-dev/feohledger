"""Migration 0151 against a real control plane: the column, the backfill from
the hierarchy, a re-run, the downgrade, and a no-op on a tenant database.

The realdb harness builds its control plane with ``create_all``, so
``is_partner`` exists there without the revision ever running. This drops it
back to the previous schema, links tenant "b" under "a", and proves the upgrade
grants the capability to exactly the org that already parents a child
(decisions §574). Always left upgraded and unlinked so later tests see the
current schema. Whole-schema equality with the model is
``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0151_org_is_partner.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0151", REVISION)
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


async def _column(engine) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'organizations' "
                    "AND column_name = 'is_partner'"
                )
            )
        ).first()
    return None if row is None else {"nullable": row[0], "default": row[1]}


async def _flags(engine, *org_ids) -> dict:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, is_partner FROM organizations WHERE id = ANY(:ids)"),
                {"ids": list(org_ids)},
            )
        ).all()
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_0151_adds_the_column_and_backfills_existing_partners(realdb):
    mig = _load()
    a, b = realdb.info("a").org_id, realdb.info("b").org_id
    engine = create_async_engine(realdb.control_db_url())
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE organizations DROP COLUMN IF EXISTS is_partner"))
            await conn.execute(
                text("UPDATE organizations SET parent_org_id = :a WHERE id = :b"), {"a": a, "b": b}
            )
        assert await _column(engine) is None

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _column(engine) == {"nullable": "NO", "default": "false"}
        # "a" parents "b", so "a" already acts as a partner; "b" does not.
        assert await _flags(engine, a, b) == {a: True, b: False}

        await _run(engine, mig.downgrade)
        await _run(engine, mig.downgrade)  # idempotent
        assert await _column(engine) is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE organizations SET parent_org_id = NULL WHERE id = :b"), {"b": b}
            )
        # Re-added with its default: every org back to false, as `create_all` left it.
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0151_is_a_no_op_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.upgrade)
        async with engine.connect() as conn:
            present = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'organizations'"
                    )
                )
            ).scalar()
        assert present is None
        await _run(engine, mig.downgrade)
    finally:
        await engine.dispose()
