"""A deleted invoice's documents leave object storage too (decisions §592).

Three paths, one rule — a folder goes only when no live invoice owns it:

* the delete queues ``{org}/{invoice}/`` and ``{org}/chat/{invoice}/`` in its
  own transaction (``pending_object_deletions``) and a post-commit job empties
  them, so a rolled-back delete keeps every object and a storage failure keeps
  a durable row;
* the retention sweep retries the rows that job could not finish;
* the retention sweep and every erasure walk one bounded page of the tenant's
  folders and delete the ones no row owns, past a grace period.

Storage is faked at ``services/storage``'s public seams (``delete_prefix``,
``list_child_prefixes``, ``newest_object_time``); their own boto3 behaviour is
pinned at the bottom against a mocked client, and in
``test_storage_delete_prefix.py`` / ``test_storage_nonblocking.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.pending_object_deletion import PendingObjectDeletion
from app.models.workflow import AuditLog
from app.services import invoice_objects, storage
from app.services.post_commit import drain_post_commit

TENANT = "a"


# ---------------------------------------------------------------------------
# Pure: which folders an invoice owns, and which folders parse as one.
# ---------------------------------------------------------------------------


def test_an_invoice_owns_its_document_folder_and_its_chat_folder():
    org, inv = uuid.uuid4(), uuid.uuid4()
    assert invoice_objects.invoice_object_prefixes(org, inv) == (
        f"{org}/{inv}/",
        f"{org}/chat/{inv}/",
    )


def test_only_invoice_shaped_folders_under_the_org_parse():
    org, inv = uuid.uuid4(), uuid.uuid4()
    parse = invoice_objects._invoice_id_of
    assert parse(org, f"{org}/{inv}/") == inv
    assert parse(org, f"{org}/chat/{inv}/") == inv
    # Another organisation's folder, a named segment, a deeper path, a key, a
    # non-uuid: none is an invoice folder of this org, so none may be deleted.
    assert parse(org, f"{uuid.uuid4()}/{inv}/") is None
    for other in ("contracts", "expenses", "tax-forms", "w9", "positive-pay", "chat"):
        assert parse(org, f"{org}/{other}/") is None
    assert parse(org, f"{org}/contracts/{inv}/") is None
    assert parse(org, f"{org}/chat/{inv}/{uuid.uuid4()}/") is None
    assert parse(org, f"{org}/{inv}") is None
    assert parse(org, f"{org}/not-a-uuid/") is None


# ---------------------------------------------------------------------------
# Real database.
# ---------------------------------------------------------------------------


async def _seed_invoice(mk, org_id, *, number: str, file_key: str | None = None) -> uuid.UUID:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Object Purge Vendor",
            amount=Decimal("120.00"),
            currency="USD",
            status=InvoiceStatus.new,
            file_key=file_key,
        )
        s.add(inv)
        await s.commit()
        return inv.id


def _record_deletes(monkeypatch, *, removed: int = 2, fail: bool = False) -> list[str]:
    calls: list[str] = []

    async def _fake(prefix: str) -> int:
        calls.append(prefix)
        if fail:
            raise ConnectionError("storage down")
        return removed

    monkeypatch.setattr(storage, "delete_prefix", _fake)
    return calls


async def _purge_audits(mk) -> list[AuditLog]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == invoice_objects.AUDIT_ACTION)
                )
            )
            .scalars()
            .all()
        )


async def _pending(mk) -> list[PendingObjectDeletion]:
    async with mk() as s:
        return list((await s.execute(select(PendingObjectDeletion))).scalars().all())


@pytest.mark.asyncio
async def test_deleting_an_invoice_empties_both_its_folders_after_commit(realdb, monkeypatch):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _seed_invoice(
        mk, org_id, number="OBJ-DEL-1", file_key=f"{org_id}/placeholder/peppol-inbound.xml"
    )
    calls = _record_deletes(monkeypatch, removed=3)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv}")
    assert resp.status_code == 204, resp.text
    await drain_post_commit()

    assert sorted(calls) == sorted(invoice_objects.invoice_object_prefixes(org_id, inv))
    # Emptied, so the outbox holds nothing for it.
    assert await _pending(mk) == []
    audits = await _purge_audits(mk)
    assert len(audits) == 1
    details = audits[0].details
    assert details["trigger"] == "invoice_deleted"
    assert details["folders_purged"] == 2 and details["objects_deleted"] == 6
    # Counts only: no key, folder or id of the invoice in the row.
    assert str(inv) not in str(details)


@pytest.mark.asyncio
async def test_bulk_delete_queues_every_deleted_invoice(realdb, monkeypatch):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    a = await _seed_invoice(mk, org_id, number="OBJ-BULK-A")
    b = await _seed_invoice(mk, org_id, number="OBJ-BULK-B")
    calls = _record_deletes(monkeypatch, removed=0)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post("/api/invoices/bulk/delete", json={"ids": [str(a), str(b)]})
    assert resp.status_code == 200, resp.text
    await drain_post_commit()

    expected = invoice_objects.invoice_object_prefixes(
        org_id, a
    ) + invoice_objects.invoice_object_prefixes(org_id, b)
    assert sorted(calls) == sorted(expected)
    assert await _pending(mk) == []
    # Nothing was in storage, so no append-only row was written for it.
    assert await _purge_audits(mk) == []


@pytest.mark.asyncio
async def test_a_storage_failure_leaves_a_durable_row_the_sweep_retries(realdb, monkeypatch):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _seed_invoice(mk, org_id, number="OBJ-FAIL-1")
    _record_deletes(monkeypatch, fail=True)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/invoices/{inv}")
    # The invoice delete itself never waits on, or fails with, storage.
    assert resp.status_code == 204, resp.text
    await drain_post_commit()

    rows = await _pending(mk)
    assert sorted(r.prefix for r in rows) == sorted(
        invoice_objects.invoice_object_prefixes(org_id, inv)
    )
    assert all(r.attempts == 1 and r.last_error == "storage_error" for r in rows)
    assert all(r.last_attempt_at is not None for r in rows)

    # Storage is back: the retention sweep's retry empties them and clears the rows.
    calls = _record_deletes(monkeypatch, removed=1)
    from app.services.retention_sweep import sweep_tenant

    async with mk() as db:
        result = await sweep_tenant(db, organization_id=org_id, settings_dict={})
        await db.commit()
    assert sorted(calls) == sorted(invoice_objects.invoice_object_prefixes(org_id, inv))
    assert result.invoice_object_folders_purged == 2
    assert result.invoice_object_folders_failed == 0
    assert await _pending(mk) == []
    audits = await _purge_audits(mk)
    assert [a.details["trigger"] for a in audits] == ["retry"]


@pytest.mark.asyncio
async def test_a_rolled_back_delete_queues_nothing_and_deletes_nothing(realdb, monkeypatch):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _seed_invoice(mk, org_id, number="OBJ-RB-1")
    calls = _record_deletes(monkeypatch)

    async with mk() as db:
        await invoice_objects.queue_invoice_objects(db, inv)
        await db.rollback()
    await drain_post_commit()

    assert calls == []
    assert await _pending(mk) == []


@pytest.mark.asyncio
async def test_a_queued_folder_a_live_invoice_still_owns_is_refused(realdb, monkeypatch):
    """Fail closed: a row naming a live invoice's folder is dropped, the objects
    stay, and the refusal is audited."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    live = await _seed_invoice(mk, org_id, number="OBJ-LIVE-1")
    async with mk() as s:
        s.add(
            PendingObjectDeletion(
                organization_id=org_id,
                prefix=f"{org_id}/{live}/",
                owner_type="invoice",
                owner_id=live,
            )
        )
        # A folder outside this org, which no queued row may reach either.
        s.add(
            PendingObjectDeletion(
                organization_id=org_id,
                prefix=f"{uuid.uuid4()}/{uuid.uuid4()}/",
                owner_type="invoice",
                owner_id=uuid.uuid4(),
            )
        )
        await s.commit()
    calls = _record_deletes(monkeypatch)

    async with mk() as db:
        result = await invoice_objects.drain_pending(db, organization_id=org_id, limit=10)
        await db.commit()

    assert calls == []
    assert result.refused == 2 and result.folders_purged == 0
    assert await _pending(mk) == []
    assert [a.details["refused"] for a in await _purge_audits(mk)] == [2]


@pytest.mark.asyncio
async def test_a_folder_holding_a_live_invoices_document_is_refused(realdb, monkeypatch):
    """Belt and braces: no path files one invoice's document under another's
    id today, but if one did, that live document keeps the folder."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    gone = uuid.uuid4()
    await _seed_invoice(mk, org_id, number="OBJ-SHARE-1", file_key=f"{org_id}/{gone}/doc.pdf")
    async with mk() as s:
        s.add(
            PendingObjectDeletion(
                organization_id=org_id,
                prefix=f"{org_id}/{gone}/",
                owner_type="invoice",
                owner_id=gone,
            )
        )
        await s.commit()
    calls = _record_deletes(monkeypatch)

    async with mk() as db:
        result = await invoice_objects.drain_pending(db, organization_id=org_id, limit=10)
        await db.commit()
    assert calls == [] and result.refused == 1


def _fake_bucket(monkeypatch, folders: dict[str, dict[str, datetime | None]]):
    """`folders[base]` maps each child folder to its newest object's time."""
    ages: dict[str, datetime | None] = {}
    for children in folders.values():
        ages.update(children)

    async def _list(prefix, *, cursor=None, limit=1000):
        return list(folders.get(prefix, {})), None

    async def _newest(prefix, *, limit=1000):
        return ages[prefix]

    monkeypatch.setattr(storage, "list_child_prefixes", _list)
    monkeypatch.setattr(storage, "newest_object_time", _newest)


@pytest.mark.asyncio
async def test_the_orphan_scan_deletes_only_old_unowned_invoice_folders(realdb, monkeypatch):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    live = await _seed_invoice(mk, org_id, number="OBJ-ORPH-LIVE")
    old_orphan, fresh_orphan, chat_orphan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=3)
    _fake_bucket(
        monkeypatch,
        {
            f"{org_id}/": {
                f"{org_id}/{live}/": long_ago,
                f"{org_id}/{old_orphan}/": long_ago,
                # Written an hour ago: may be an upload whose row is in flight.
                f"{org_id}/{fresh_orphan}/": now - timedelta(hours=1),
                # Named folders belong to other owners and are never candidates.
                f"{org_id}/contracts/": long_ago,
                f"{org_id}/chat/": long_ago,
            },
            f"{org_id}/chat/": {
                f"{org_id}/chat/{chat_orphan}/": long_ago,
                f"{org_id}/chat/{live}/": long_ago,
            },
        },
    )
    calls = _record_deletes(monkeypatch, removed=1)

    async with mk() as db:
        result = await invoice_objects.sweep_orphaned_invoice_objects(
            db, organization_id=org_id, now=now
        )
        await db.commit()

    assert sorted(calls) == sorted([f"{org_id}/{old_orphan}/", f"{org_id}/chat/{chat_orphan}/"])
    assert result.folders_purged == 2 and result.skipped_fresh == 1
    audits = await _purge_audits(mk)
    assert [a.details["trigger"] for a in audits] == ["orphan_scan"]
    assert str(old_orphan) not in str(audits[0].details)


@pytest.mark.asyncio
async def test_an_unreadable_bucket_fails_the_pass_softly(realdb, monkeypatch):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async def _down(prefix, *, cursor=None, limit=1000):
        raise ConnectionError("storage down")

    monkeypatch.setattr(storage, "list_child_prefixes", _down)
    calls = _record_deletes(monkeypatch)
    async with mk() as db:
        result = await invoice_objects.sweep_orphaned_invoice_objects(db, organization_id=org_id)
        await db.commit()
    assert calls == [] and result.failed == 2
    assert await _purge_audits(mk) == []


@pytest.mark.asyncio
async def test_an_erasure_reaches_orphaned_invoice_folders(realdb, monkeypatch):
    """No row leads the per-subject walk to a deleted invoice's document, so the
    erasure runs the same orphan pass and counts what it removed."""
    from app.models.data_subject_request import SUBJECT_VENDOR_CONTACT
    from app.services.privacy_erasure import ErasureResult, _erase_documents

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    orphan = uuid.uuid4()
    _fake_bucket(
        monkeypatch,
        {f"{org_id}/": {f"{org_id}/{orphan}/": datetime.now(UTC) - timedelta(days=2)}},
    )
    calls = _record_deletes(monkeypatch, removed=4)

    result = ErasureResult()
    async with mk() as db:
        await _erase_documents(
            subject_type=SUBJECT_VENDOR_CONTACT,
            subject_id=uuid.uuid4(),
            organization_id=org_id,
            tenant_db=db,
            result=result,
            now=datetime.now(UTC),
        )
        await db.commit()

    assert calls == [f"{org_id}/{orphan}/"]
    assert result.orphaned_objects_deleted == 4
    assert result.documents_failed == 0
    assert [a.details["trigger"] for a in await _purge_audits(mk)] == ["erasure"]


# ---------------------------------------------------------------------------
# The storage listing helpers, against a mocked boto3 client.
# ---------------------------------------------------------------------------


@pytest.mark.live_orphan_listing
@pytest.mark.asyncio
async def test_list_child_prefixes_returns_one_page_and_a_continuation():
    client = MagicMock()
    client.list_objects_v2 = MagicMock(
        return_value={
            "CommonPrefixes": [{"Prefix": "o/a/"}, {"Prefix": "o/b/"}],
            "IsTruncated": True,
            "NextContinuationToken": "tok",
        }
    )
    with patch.object(storage, "_get_client", return_value=client):
        children, cursor = await storage.list_child_prefixes("o/", cursor="prev", limit=2)
    assert children == ["o/a/", "o/b/"] and cursor == "tok"
    kwargs = client.list_objects_v2.call_args.kwargs
    assert kwargs["Delimiter"] == "/" and kwargs["MaxKeys"] == 2
    assert kwargs["ContinuationToken"] == "prev" and kwargs["Prefix"] == "o/"


@pytest.mark.live_orphan_listing
@pytest.mark.asyncio
async def test_list_child_prefixes_guards_and_missing_bucket():
    client = MagicMock()
    client.list_objects_v2 = MagicMock(
        side_effect=ClientError({"Error": {"Code": "NoSuchBucket"}}, "ListObjectsV2")
    )
    with patch.object(storage, "_get_client", return_value=client):
        for bad in ("", "  ", "o"):
            with pytest.raises(ValueError):
                await storage.list_child_prefixes(bad)
        assert await storage.list_child_prefixes("o/") == ([], None)


@pytest.mark.asyncio
async def test_newest_object_time_reads_one_page_and_fails_closed_when_truncated():
    t1, t2 = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    client = MagicMock()
    client.list_objects_v2 = MagicMock(
        return_value={"Contents": [{"LastModified": t1}, {"LastModified": t2}]}
    )
    with patch.object(storage, "_get_client", return_value=client):
        assert await storage.newest_object_time("o/x/") == t2
        client.list_objects_v2.return_value = {"Contents": []}
        assert await storage.newest_object_time("o/x/") is None
        # Too large to read in one page: reported as fresh, so it is left alone.
        client.list_objects_v2.return_value = {"Contents": [], "IsTruncated": True}
        assert await storage.newest_object_time("o/x/") > datetime.now(UTC) - timedelta(minutes=1)
        with pytest.raises(ValueError):
            await storage.newest_object_time("")
