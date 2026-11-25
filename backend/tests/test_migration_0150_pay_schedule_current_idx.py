"""Migration 0150 runs against a real tenant database: down, up, and up again.

The realdb harness builds tenants with ``create_all``, so the index exists there
without the revision ever executing; that is the fresh-tenant half of the
parity this pins (the model's ``Index`` must produce 0150's exact definition).
The other half: the revision adds the index to a tenant at the previous schema,
re-runs harmlessly, drops it on downgrade, and no-ops on the control plane.
Always left upgraded so later tests see the current schema.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.payment import PaymentSchedule

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0150_pay_schedule_current_idx.py"
INDEX = "ix_payment_schedules_invoice_current"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0150", REVISION)
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


async def _index_def(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n"
                ),
                {"n": INDEX},
            )
        ).scalar_one_or_none()


def test_the_revision_and_the_model_name_the_same_index():
    assert _load().INDEX_NAME == INDEX
    model = {ix.name: ix for ix in PaymentSchedule.__table__.indexes}
    assert INDEX in model
    # The plain FK index stays beside it (fresh and migrated tenants agree).
    assert "ix_payment_schedules_invoice_id" in model


@pytest.mark.asyncio
async def test_0150_adds_the_index_on_a_tenant_and_reverses(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        # create_all (the harness, and every freshly provisioned tenant) built it.
        fresh = await _index_def(engine)
        assert fresh is not None
        assert "(invoice_id, created_at DESC, id DESC)" in fresh and "UNIQUE" not in fresh

        await _run(engine, mig.downgrade)
        assert await _index_def(engine) is None
        await _run(engine, mig.downgrade)  # dropping an absent index is a no-op

        await _run(engine, mig.upgrade)
        assert await _index_def(engine) == fresh

        await _run(engine, mig.upgrade)  # a second upgrade changes nothing
        assert await _index_def(engine) == fresh
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0150_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        async with engine.connect() as conn:
            has_schedules = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'payment_schedules'"
                    )
                )
            ).scalar()
        assert has_schedules is None  # the gate's premise
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        assert await _index_def(engine) is None
    finally:
        await engine.dispose()
