"""Migration 0140 runs against a real control plane: down, up, up again — and
builds exactly what ``create_all`` builds; on a tenant database it is a no-op.

The realdb harness builds the control plane with ``create_all``, so the two
``subscriptions`` columns (decisions §483) exist there without the revision
running. This file captures them and their foreign key, drives the revision
down and up twice through Alembic's ``Operations``, and compares, so an
existing control plane migrated to head and a fresh one hold the same schema.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REVISION = Path(__file__).resolve().parents[1] / (
    "alembic/versions/0140_subscription_pending_plan.py"
)
COLUMNS = ("pending_plan_id", "pending_plan_effective_at")


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0140", REVISION)
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


async def _schema(engine) -> dict:
    async with engine.connect() as conn:
        columns = {
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'subscriptions' AND column_name = ANY(:cols)"
                ),
                {"cols": list(COLUMNS)},
            )
        }
        constraints = {
            tuple(r)
            for r in await conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = to_regclass('public.subscriptions') "
                    "AND pg_get_constraintdef(oid) LIKE '%pending_plan_id%'"
                )
            )
        }
    return {"columns": columns, "constraints": constraints}


def test_revision_identity():
    mig = _load()
    assert mig.revision == "0140_subscription_pending_plan"
    assert mig.down_revision == "0139_sanctions_false_positive"
    assert len(mig.revision) <= 32


@pytest.mark.asyncio
async def test_0140_matches_create_all_and_reruns_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        built = await _schema(engine)  # what create_all builds
        assert {c[0] for c in built["columns"]} == set(COLUMNS)
        assert {c[2] for c in built["columns"]} == {"YES"}  # both nullable
        assert built["constraints"] == {
            (
                "subscriptions_pending_plan_id_fkey",
                "FOREIGN KEY (pending_plan_id) REFERENCES plans(id)",
            )
        }

        await _run(engine, mig.downgrade)
        assert await _schema(engine) == {"columns": set(), "constraints": set()}
        await _run(engine, mig.downgrade)  # idempotent

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _schema(engine) == built
    finally:
        # Leave the slot's control plane as create_all built it.
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0140_is_a_no_op_on_a_tenant_database(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        async with engine.connect() as conn:
            before = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).scalar_one()
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        async with engine.connect() as conn:
            after = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).scalar_one()
        assert after == before
    finally:
        await engine.dispose()
