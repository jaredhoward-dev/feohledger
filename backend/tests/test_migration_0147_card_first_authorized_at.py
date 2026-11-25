"""Migration 0147 runs against a real tenant: it adds the nullable
``virtual_cards.first_authorized_at`` (decisions §549), backfills it from
``charged_at`` and only from there, a re-run is harmless, it reverses, and the
control plane is untouched.

The realdb harness builds each tenant with ``create_all``, so the column
already exists there; the test seeds its cards first, drops the column to stand
at 0146, and always leaves it upgraded so later tests see the current schema.
Whole-schema equality with the model is ``test_schema_build_parity.py``'s job.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_card_money_path_regression import _card_row, _seed_invoice

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0147_card_first_authorized_at.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0147", REVISION)
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
                    "WHERE table_schema = 'public' AND table_name = 'virtual_cards' "
                    "AND column_name = 'first_authorized_at'"
                )
            )
        ).first()


@pytest.mark.asyncio
async def test_0147_adds_and_backfills_the_column_on_a_tenant(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    assert mig.revision == "0147_card_first_authorized_at"
    assert len(mig.revision) <= 32
    assert mig.down_revision == "0146_invoice_erp_void_pending"

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    charged_at = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    inv_charged = await _seed_invoice(mk, org_id, amount=Decimal("100.00"))
    inv_unused = await _seed_invoice(mk, org_id, amount=Decimal("100.00"))
    async with mk() as s:
        charged = _card_row(
            inv_charged,
            org_id,
            provider_card_id=f"tok_{uuid.uuid4().hex[:10]}",
            limit=Decimal("100.00"),
            status="charged",
            amount_charged=Decimal("100.00"),
        )
        charged.charged_at = charged_at
        unused = _card_row(
            inv_unused,
            org_id,
            provider_card_id=f"tok_{uuid.uuid4().hex[:10]}",
            limit=Decimal("100.00"),
        )
        s.add_all([charged, unused])
        await s.commit()
        charged_id, unused_id = charged.id, unused.id

    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        await _run(engine, mig.downgrade)  # stand at 0146
        assert await _column(engine) is None
        await _run(engine, mig.downgrade)  # idempotent down
        await _run(engine, mig.upgrade)
        assert tuple(await _column(engine)) == ("timestamp with time zone", "YES")
        await _run(engine, mig.upgrade)  # idempotent
        async with engine.connect() as conn:
            rows = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT id, first_authorized_at FROM virtual_cards WHERE id IN (:a, :b)"
                        ),
                        {"a": charged_id, "b": unused_id},
                    )
                ).all()
            )
        assert rows[charged_id] == charged_at  # authorized at its charge
        assert rows[unused_id] is None  # never authorized: nothing invented
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0147_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        async with engine.connect() as conn:
            exists = (
                await conn.execute(text("SELECT to_regclass('public.virtual_cards') IS NOT NULL"))
            ).scalar()
        assert not exists
    finally:
        await engine.dispose()
