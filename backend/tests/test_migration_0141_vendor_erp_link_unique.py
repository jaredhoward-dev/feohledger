"""Migration 0141 — one vendor per ERP record per company (decisions §486).

The realdb harness builds every tenant with ``create_all``, so
``uq_vendors_erp_link`` exists there without the revision running: the
fresh-tenant half of the parity (the model must declare the index the revision
builds). The other half, on a tenant at the previous schema (no index, two
vendors holding one record):

* the revision REFUSES while such a pair exists, says how many, names the
  vendors by row id only (never a name or the ERP company), and changes nothing;
* once the operator has resolved the pair it builds exactly the index
  ``create_all`` builds, and re-runs harmlessly;
* it is a no-op on the control plane.

Always left upgraded, so later tests see the head schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.vendor import Vendor

REVISION = Path(__file__).resolve().parents[1] / "alembic/versions/0141_vendor_erp_link_unique.py"
TENANT = "a"


def _load():
    spec = importlib.util.spec_from_file_location("_mig_0141", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chains_after_0140_and_fits_the_version_column():
    mig = _load()
    assert mig.down_revision == "0140_subscription_pending_plan"
    assert len(mig.revision) <= 32
    assert REVISION.stem == mig.revision


def test_the_model_declares_the_index_the_revision_builds():
    """Fresh tenants are built by ``create_all``, not Alembic."""
    declared = {i.name: i for i in Vendor.__table__.indexes}
    index = declared[_load().INDEX]
    assert index.unique
    assert [c.name for c in index.columns] == [
        "organization_id",
        "erp_link_realm",
        "erp_vendor_id",
    ]
    assert index.dialect_options["postgresql"]["nulls_not_distinct"] is True
    assert str(index.dialect_options["postgresql"]["where"]) == "erp_vendor_id IS NOT NULL"


async def _run(engine, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


async def _indexdef(engine, name: str) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": name}
            )
        ).scalar_one_or_none()


async def _insert(engine, org_id, rows: list[tuple[uuid.UUID, str, str | None]]) -> None:
    async with async_sessionmaker(engine)() as s:
        s.add_all(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name=name,
                status="active",
                source="erp_sync",
                erp_vendor_id="57",
                erp_link_realm=realm,
            )
            for vendor_id, name, realm in rows
        )
        await s.commit()


async def test_0141_refuses_a_shared_link_then_builds_the_create_all_index(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    info = realdb.info(TENANT)
    engine = create_async_engine(_make_tenant_url(info.db_name))
    tag = uuid.uuid4().hex[:10]
    legacy_a, legacy_b, named_a, named_b, other_company = (uuid.uuid4() for _ in range(5))
    realm = f"xero:{tag}"
    try:
        built = await _indexdef(engine, mig.INDEX)  # create_all's
        assert built is not None and "UNIQUE" in built and "NULLS NOT DISTINCT" in built
        await _run(engine, mig.downgrade)
        assert await _indexdef(engine, mig.INDEX) is None

        # The previous schema admits two holders of "57" with no recorded
        # company, two under one named company, and one under another company
        # (which is a different record, not a duplicate).
        await _insert(
            engine,
            info.org_id,
            [
                (legacy_a, f"Legacy {tag}", None),
                (legacy_b, f"Legacy {tag} Ltd", None),
                (named_a, f"Named {tag}", realm),
                (named_b, f"Named {tag} Ltd", realm),
                (other_company, f"Other {tag}", f"xero:other-{tag}"),
            ],
        )

        with pytest.raises(mig.DuplicateErpVendorLink) as exc:
            await _run(engine, mig.upgrade)
        message = str(exc.value)
        assert "2 ERP vendor link(s) shared by 4 vendors" in message
        assert info.db_name in message
        assert "vendor-management.md" in message
        for vendor_id in (legacy_a, legacy_b, named_a, named_b):
            assert str(vendor_id) in message
        assert str(other_company) not in message
        assert tag not in message  # no vendor name, no ERP company
        assert await _indexdef(engine, mig.INDEX) is None  # refused before anything changed

        # The operator's call: unlink the stale holder in each group.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE vendors SET erp_vendor_id = NULL, erp_link_realm = NULL "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": [legacy_b, named_b]},
            )

        await _run(engine, mig.upgrade)
        await _run(engine, mig.upgrade)  # idempotent
        assert await _indexdef(engine, mig.INDEX) == built
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM vendors WHERE id = ANY(:ids)"),
                {"ids": [legacy_a, legacy_b, named_a, named_b, other_company]},
            )
        await _run(engine, mig.upgrade)
        await engine.dispose()


async def test_0141_names_at_most_ten_vendors(realdb):
    from app.database import _make_tenant_url

    mig = _load()
    info = realdb.info(TENANT)
    engine = create_async_engine(_make_tenant_url(info.db_name))
    ids = [uuid.uuid4() for _ in range(12)]
    try:
        await _run(engine, mig.downgrade)
        await _insert(engine, info.org_id, [(i, "Many", None) for i in ids])
        with pytest.raises(mig.DuplicateErpVendorLink) as exc:
            await _run(engine, mig.upgrade)
        message = str(exc.value)
        assert "1 ERP vendor link(s) shared by 12 vendors" in message
        assert sum(str(i) in message for i in ids) == mig.NAMED_IDS
        assert "and 2 more" in message
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM vendors WHERE id = ANY(:ids)"), {"ids": ids})
        await _run(engine, mig.upgrade)
        await engine.dispose()


async def test_0141_is_a_no_op_on_the_control_plane(realdb):
    mig = _load()
    engine = create_async_engine(realdb.control_db_url())
    try:
        await _run(engine, mig.upgrade)
        await _run(engine, mig.downgrade)
        assert await _indexdef(engine, mig.INDEX) is None
    finally:
        await engine.dispose()
