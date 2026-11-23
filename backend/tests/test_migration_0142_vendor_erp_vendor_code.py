"""Migration 0142 — the ERP's vendor number gets its own column (decisions §490).

The realdb harness builds every tenant with ``create_all``, so the column
exists there without the revision running (the model declares it). On a
tenant at the previous schema the revision:

* adds ``erp_vendor_code`` and moves ``code`` into it on the rows the sync
  created (``source = 'erp_sync'`` with no onboarding user), whose code the
  old sync wrote;
* leaves every other row's ``code`` alone (a person's vendor, linked or not,
  and a stub a person onboarded that the sync later adopted);
* re-runs harmlessly, and is a no-op on the control plane.

Always left upgraded, so later tests see the head schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.vendor import Vendor

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0142_vendor_erp_vendor_code.py"
TENANT = "a"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0142", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chains_after_0141_and_fits_the_version_column():
    mig = _load()
    assert mig.down_revision == "0141_vendor_erp_link_unique"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


def test_the_model_declares_the_column_the_revision_adds():
    column = Vendor.__table__.c.erp_vendor_code
    assert column.nullable and column.type.length == 50


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _has_column(engine) -> bool:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'vendors' AND column_name = 'erp_vendor_code'"
                )
            )
        ).scalar_one_or_none() is not None


async def test_0142_moves_only_the_codes_the_sync_wrote(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    info = realdb.info(TENANT)
    engine = create_async_engine(_make_tenant_url(info.db_name))
    onboarder = uuid.uuid4()
    synced, adopted_stub, linked_manual, manual = (uuid.uuid4() for _ in range(4))
    rows = [
        # (id, code, source, erp_vendor_id, onboarded_by_user_id)
        (synced, "BC-1", "erp_sync", "E-1", None),
        (adopted_stub, "LEGACY-9", "erp_sync", "E-2", onboarder),
        (linked_manual, "MINE", "manual", "E-3", None),
        (manual, "P-1", "manual", None, None),
    ]
    try:
        # Seeded at head (the ORM knows every NOT NULL default), with the new
        # column empty, then taken back to the previous schema.
        async with async_sessionmaker(engine)() as s:
            s.add_all(
                Vendor(
                    id=vendor_id,
                    organization_id=info.org_id,
                    name=f"Mig 0142 {vendor_id.hex[:8]}",
                    code=code,
                    status="active",
                    source=source,
                    erp_vendor_id=erp_id,
                    onboarded_by_user_id=onboarded,
                )
                for vendor_id, code, source, erp_id, onboarded in rows
            )
            await s.commit()
        await _run(engine, mig.downgrade)
        assert not await _has_column(engine)

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent

        async with engine.connect() as conn:
            got = {
                r.id: (r.code, r.erp_vendor_code)
                for r in await conn.execute(
                    text("SELECT id, code, erp_vendor_code FROM vendors WHERE id = ANY(:ids)"),
                    {"ids": [r[0] for r in rows]},
                )
            }
        assert got == {
            synced: (None, "BC-1"),
            adopted_stub: ("LEGACY-9", None),
            linked_manual: ("MINE", None),
            manual: ("P-1", None),
        }
    finally:
        await _run(engine, mig.upgrade)
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM vendors WHERE id = ANY(:ids)"), {"ids": [r[0] for r in rows]}
            )
        await engine.dispose()


async def test_0142_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
    finally:
        await engine.dispose()
