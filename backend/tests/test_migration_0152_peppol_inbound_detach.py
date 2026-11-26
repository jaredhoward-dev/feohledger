"""Migration 0152 runs against a real tenant database: down, up, and up again.

The realdb harness builds tenants with ``create_all``, so the columns and the
check exist there without the revision ever executing; that is the
fresh-tenant half of the parity this pins. The other half: the revision brings
a tenant at the previous schema to the same shape, re-runs harmlessly, refuses
to downgrade over a detached row (it would have to delete a received
e-invoice's record), and no-ops on the control plane. Always left upgraded so
later tests see the current schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.peppol_transmission import PeppolTransmission

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0152_peppol_inbound_detach.py"
CONSTRAINT = "ck_peppol_detached_inbound"
COLUMNS = ("invoice_deleted_at", "invoice_deleted_by_id", "deleted_invoice_id")


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0152", REVISION)
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


async def _shape(engine) -> dict:
    async with engine.connect() as conn:
        cols = dict(
            (
                await conn.execute(
                    text(
                        "SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'peppol_transmissions'"
                    )
                )
            ).all()
        )
        check = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = :n AND conrelid = 'public.peppol_transmissions'::regclass"
                ),
                {"n": CONSTRAINT},
            )
        ).scalar_one_or_none()
    return {
        "invoice_id_nullable": cols.get("invoice_id"),
        "columns": sorted(c for c in COLUMNS if c in cols),
        "check": check,
    }


def test_the_revision_and_the_model_agree():
    mig = _load()
    assert mig.CONSTRAINT == CONSTRAINT
    table = PeppolTransmission.__table__
    assert table.c.invoice_id.nullable is True
    for col in COLUMNS:
        assert table.c[col].nullable is True
    (model_check,) = [c for c in table.constraints if c.name == CONSTRAINT]
    assert str(model_check.sqltext) == mig.CHECK


@pytest.mark.asyncio
async def test_0152_shapes_a_tenant_and_reverses(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        fresh = await _shape(engine)  # create_all built it
        assert fresh["invoice_id_nullable"] == "YES"
        assert fresh["columns"] == sorted(COLUMNS)
        assert fresh["check"] is not None

        await _run(engine, mig.downgrade)
        down = await _shape(engine)
        assert down == {"invoice_id_nullable": "NO", "columns": [], "check": None}
        await _run(engine, mig.downgrade)  # a second downgrade is a no-op

        await _run(engine, mig.upgrade)
        assert await _shape(engine) == fresh
        await _run(engine, mig.upgrade)  # a second upgrade changes nothing
        assert await _shape(engine) == fresh
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0152_refuses_to_downgrade_over_a_detached_row(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    row_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PeppolTransmission(
                id=row_id,
                invoice_id=None,
                direction="inbound",
                participant_scheme="9930",
                participant_value="SUPPLIER123",
                doc_type_id="doc",
                process_id="proc",
                business_message_id=uuid.uuid4().hex,
                status="delivered",
                provider="mock",
                organization_id=org_id,
                invoice_deleted_at=text("now()"),
                deleted_invoice_id=uuid.uuid4(),
            )
        )
        await s.commit()

    engine = create_async_engine(_make_tenant_url(realdb.info("a").db_name))
    try:
        with pytest.raises(RuntimeError, match="cannot be downgraded"):
            await _run(engine, mig.downgrade)
        assert (await _shape(engine))["columns"] == sorted(COLUMNS)
    finally:
        await _run(engine, mig.upgrade)
        await engine.dispose()


@pytest.mark.asyncio
async def test_0152_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        await _run(engine, mig.upgrade)
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = 'peppol_transmissions'"
                    )
                )
            ).scalar() is None
    finally:
        await engine.dispose()
