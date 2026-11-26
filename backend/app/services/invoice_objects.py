"""A deleted invoice's documents leave object storage too (decisions §592).

**Where an invoice's objects live.** Every object an invoice owns sits under one
of two folders, by construction of every key-writing site
(``tests/test_storage_prefix_ownership.py`` holds the org-first half of that):

* ``{org}/{invoice}/`` — the invoice document from every channel: AP upload,
  attach and replace (``storage.upload_invoice_file``, versioned keys included,
  so every superseded version §363 kept), the supplier portal submit and
  resubmit, email intake (``services/email_intake``) and the received PEPPOL
  payload ``{org}/{invoice}/peppol-inbound.xml`` (``services/peppol_receive``).
* ``{org}/chat/{invoice}/`` — supplier-chat attachments, either author
  (``storage.upload_chat_file``).

Nothing else is keyed by an invoice id: contracts, expenses, tax forms, vendor
statements and Positive Pay files have their own named segment and their own
owning row. Extraction writes no object.

**Why folders, not keys.** The keys an invoice held are not all on its row: a
superseded version is named only by an ``audit_log`` row, which in ``lambda``
audit mode is not in the tenant database at all. A folder needs no row to be
complete, and ``storage.delete_prefix`` already refuses the two arguments that
would make it unbounded.

**Three paths, one rule — delete only a folder no live invoice owns:**

1. :func:`queue_invoice_objects` — called from ``_delete_invoice_cascade`` in
   the delete's own transaction. It writes one ``pending_object_deletions`` row
   per folder (the outbox) and queues ONE ``post_commit`` job per transaction
   that empties every row the transaction wrote (a bulk delete included) once
   it has committed. A rolled-back delete drops both together.
2. :func:`drain_pending` — what that job runs, and the retention sweep's and
   every erasure's retry of any row it did not finish (storage down, worker
   gone). It claims rows and reads ownership, empties the folders with no
   statement in flight, then finishes — all on the one connection of the
   session it is given, because a dispatcher's or sweep's engine may have only
   one. The job commits the claim before storage (no lock held, the lease
   visible to other drains); the sweep and erasure keep it in their own
   transaction. A row is removed only when its folder is empty.
3. :func:`sweep_orphaned_invoice_objects` — a bounded listing of the tenant's
   folders compared against live ``invoices`` rows, for folders no outbox row
   names: invoices deleted before §592, or a write whose row never committed.
   Only a folder whose newest object is older than the grace period goes, so an
   upload whose invoice row is still in flight is never touched.

Every pass that deletes anything writes one ``storage.objects_deleted`` audit
row: counts only, never a key (a key's last segment is a user's filename).
Logs carry exception class names only. All bucket I/O goes through
``services/storage``, which offloads boto3 to a worker thread.

**What this does NOT override.** The bucket's GOVERNANCE Object Lock
(``infra/s3.tf``): on a versioned, locked bucket a delete writes a delete
marker, the locked version stays unreadable to the app until its lock lapses,
and the noncurrent-version lifecycle rule reaps it after that. That is the
same outcome the Positive Pay expiry (§184) and erasure (§183) already have.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import in_dispatch_scope
from app.models.invoice import Invoice
from app.models.pending_object_deletion import OWNER_INVOICE, PendingObjectDeletion
from app.services import storage
from app.services.audit_dispatch import dispatch_audit
from app.services.post_commit import enqueue_post_commit, is_post_commit_queued

logger = logging.getLogger(__name__)

AUDIT_ACTION = "storage.objects_deleted"

#: How old the newest object in an unowned folder must be before the orphan
#: scan deletes it. An invoice row and its first upload are written in one
#: request, so anything younger may be an upload whose row has not committed.
ORPHAN_GRACE = timedelta(hours=24)

# Where the orphan scan resumes per (organisation, folder). In-process only: a
# restart begins the walk again from the start, which costs a repeat listing,
# never a missed folder — the walk always reaches the end before wrapping.
_orphan_cursors: dict[tuple[uuid.UUID, str], str | None] = {}


@dataclass
class PurgeResult:
    """Counts for one pass; the audit row and the sweep result read these."""

    folders_purged: int = 0
    objects_deleted: int = 0
    failed: int = 0
    refused: int = 0
    skipped_fresh: int = 0
    #: Outbox rows another drain holds (live claim, or row-locked right now),
    #: left to it. Not a failure, but not reached either: an erasure counts it.
    skipped_claimed: int = 0
    #: Outbox rows retaken after an earlier drain's claim lapsed unfinished.
    reclaimed: int = 0


def invoice_object_prefixes(organization_id: uuid.UUID, invoice_id: uuid.UUID) -> tuple[str, str]:
    """The two folders holding every object an invoice owns (module docstring)."""
    return (f"{organization_id}/{invoice_id}/", f"{organization_id}/chat/{invoice_id}/")


def _invoice_id_of(organization_id: uuid.UUID, prefix: str) -> uuid.UUID | None:
    """The invoice a folder belongs to, or None when it is not an invoice folder.

    Accepts exactly ``{org}/{uuid}/`` and ``{org}/chat/{uuid}/``. Anything else
    — another organisation's prefix, a named segment, a deeper path — is not
    ours to delete under this rule.
    """
    head = f"{organization_id}/"
    if not prefix.startswith(head) or not prefix.endswith("/"):
        return None
    parts = prefix[len(head) : -1].split("/")
    if len(parts) == 2 and parts[0] == "chat":
        parts = parts[1:]
    if len(parts) != 1:
        return None
    try:
        return uuid.UUID(parts[0])
    except ValueError:
        return None


async def _owned_by_a_live_invoice(db: AsyncSession, invoice_id: uuid.UUID, prefix: str) -> bool:
    """Does any live invoice still own this folder?

    The invoice itself, or — belt and braces — any row whose stored document
    key sits inside the folder. No path writes one invoice's document under
    another's id today; this is what keeps a future one from losing it.
    """
    return bool(
        (
            await db.execute(
                select(
                    exists().where(
                        or_(
                            Invoice.id == invoice_id,
                            Invoice.file_key.startswith(prefix, autoescape=True),
                        )
                    )
                )
            )
        ).scalar()
    )


@dataclass
class _Batch:
    """The outbox rows one transaction queued, drained by ONE post-commit job.

    Bulk delete calls :func:`queue_invoice_objects` once per invoice; a job and
    a session per invoice would be N sessions and N passes after the commit.
    The job reads ``rows`` when it runs, so every row queued before the commit
    is in it.
    """

    engine: object
    rows: dict[uuid.UUID, list[uuid.UUID]] = field(default_factory=dict)
    job: Callable[[], Awaitable[None]] | None = None


_BATCH_KEY = "_feoh_invoice_object_batch"


async def queue_invoice_objects(db: AsyncSession, invoice_id: uuid.UUID) -> None:
    """Queue the invoice's folders for deletion once this transaction commits.

    Must run inside the delete's transaction, before the invoice row goes (its
    organisation id is read from it). The outbox rows commit or roll back with
    the delete; the ``post_commit`` job is the fast path and the retention
    sweep the durable one. However many invoices one transaction deletes, it
    queues one job, which drains every row the transaction wrote.
    """
    organization_id = (
        await db.execute(select(Invoice.organization_id).where(Invoice.id == invoice_id))
    ).scalar_one_or_none()
    if organization_id is None:
        return
    rows = [
        PendingObjectDeletion(
            organization_id=organization_id,
            prefix=prefix,
            owner_type=OWNER_INVOICE,
            owner_id=invoice_id,
        )
        for prefix in invoice_object_prefixes(organization_id, invoice_id)
    ]
    db.add_all(rows)
    await db.flush()
    engine = db.bind
    if engine is None:  # an unbound session: the retention sweep will drain it
        return

    batch: _Batch | None = db.info.get(_BATCH_KEY)
    # A batch whose job is no longer queued belongs to a transaction that has
    # committed (its job ran or is running) or rolled back: start another.
    if in_dispatch_scope():
        # A dispatcher runs post-commit jobs inline from `after_commit`, while
        # the committing session still holds its engine's only connection
        # (`pool_size=1`): the job could never check one out. The rows are
        # durable, and the retention sweep drains them.
        return
    if batch is None or batch.job is None or not is_post_commit_queued(db, batch.job):
        batch = _Batch(engine=engine)
        batch.job = _drain_batch_job(batch)
        db.info[_BATCH_KEY] = batch
        enqueue_post_commit(db, batch.job, name="invoice_objects.purge")
    batch.rows.setdefault(organization_id, []).extend(row.id for row in rows)


def _drain_batch_job(batch: _Batch) -> Callable[[], Awaitable[None]]:
    async def _purge() -> None:
        # One session, so one connection at a time; `drain_pending` commits its
        # claim before any storage call and its finish after them.
        factory = async_sessionmaker(batch.engine, expire_on_commit=False)
        async with factory() as session:
            for organization_id, row_ids in batch.rows.items():
                await drain_pending(
                    session,
                    organization_id=organization_id,
                    row_ids=row_ids,
                    own_transactions=True,
                )

    return _purge


async def _purge_folder(
    db: AsyncSession, organization_id: uuid.UUID, prefix: str, result: PurgeResult
) -> bool | None:
    """Empty one invoice folder. True when emptied, False when refused (a live
    owner or not an invoice folder), None when storage failed."""
    invoice_id = _invoice_id_of(organization_id, prefix)
    if invoice_id is None or await _owned_by_a_live_invoice(db, invoice_id, prefix):
        result.refused += 1
        return False
    try:
        removed = await storage.delete_prefix(prefix)
    except Exception as exc:  # noqa: BLE001 — one folder must not halt the pass
        result.failed += 1
        logger.warning("[invoice-objects] folder delete failed: %s", exc.__class__.__name__)
        return None
    result.folders_purged += 1
    result.objects_deleted += removed
    return True


async def _audit(
    db: AsyncSession, organization_id: uuid.UUID, trigger: str, result: PurgeResult
) -> None:
    # One row per pass that removed, refused or retook something. An empty
    # folder emptied, or a storage failure (logged, and kept on the outbox row
    # for the next pass), is not worth an append-only row every tick. A retaken
    # claim is: the drain that claimed it may have emptied the folder and then
    # lost its own row with its finishing transaction, so this row is the only
    # record that the folder went (its objects were counted by nobody).
    if not (result.objects_deleted or result.refused or result.reclaimed):
        return
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=organization_id,
        actor_id=None,  # the deleting user is on the `invoice.deleted` row
        action=AUDIT_ACTION,
        entity_type="storage",
        entity_id=organization_id,
        details={
            # Counts only: a key's last segment is a user's filename.
            "trigger": trigger,
            "owner_type": OWNER_INVOICE,
            "folders_purged": result.folders_purged,
            "objects_deleted": result.objects_deleted,
            "failed": result.failed,
            "refused": result.refused,
            "skipped_fresh": result.skipped_fresh,
            "skipped_claimed": result.skipped_claimed,
            "reclaimed": result.reclaimed,
        },
    )


#: How long a claimed outbox row is left to the drain that claimed it. A drain
#: that dies mid-pass (worker killed, restart), or whose finishing transaction
#: fails, leaves its rows marked; after this long another drain may take them.
#: Emptying a folder is idempotent, so a drain merely slower than this costs a
#: repeat delete, never a lost row.
CLAIM_LEASE = timedelta(minutes=15)

#: ``last_error`` while a drain holds the row (between its claim and finish).
CLAIMED = "claimed"


@dataclass
class _Claim:
    row_id: uuid.UUID
    prefix: str
    refused: bool


async def _claim(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    row_ids: list[uuid.UUID] | None,
    limit: int | None,
    result: PurgeResult,
) -> list[_Claim]:
    """Take rows for this drain and decide, per folder, whether it may go.

    Runs on ``db``'s open transaction and never commits it (see
    :func:`drain_pending` for who commits). ``SKIP LOCKED`` passes over rows
    another transaction is claiming or draining right now, and the ``CLAIMED``
    marker with a fresh ``last_attempt_at`` over rows another drain claimed and
    is still working on; both are counted in ``result.skipped_claimed``, so a
    caller that must know every queued folder was reached (an erasure) can tell
    it was not. The lease is stamped and compared on THIS process's clock,
    never a caller's ``now``: a backdated ``now`` would stamp a claim that is
    already expired, and a second drain would take the row mid-delete.

    The ownership read happens here, in the same short transaction, so the
    storage calls that follow need no session at all.
    """
    stamp = datetime.now(UTC)
    scope = [PendingObjectDeletion.organization_id == organization_id]
    if row_ids is not None:
        scope.append(PendingObjectDeletion.id.in_(row_ids))
    leased = and_(
        PendingObjectDeletion.last_error == CLAIMED,
        PendingObjectDeletion.last_attempt_at >= stamp - CLAIM_LEASE,
    )
    takeable = or_(
        PendingObjectDeletion.last_error.is_distinct_from(CLAIMED),
        PendingObjectDeletion.last_attempt_at.is_(None),
        PendingObjectDeletion.last_attempt_at < stamp - CLAIM_LEASE,
    )
    stmt = (
        select(
            PendingObjectDeletion.id,
            PendingObjectDeletion.prefix,
            PendingObjectDeletion.last_error,
        )
        .where(*scope, takeable)
        .order_by(PendingObjectDeletion.created_at.asc(), PendingObjectDeletion.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (
        await db.execute(stmt.with_for_update(skip_locked=True, of=PendingObjectDeletion))
    ).all()
    taken = [row_id for row_id, _prefix, _err in rows]

    # Left to another drain: under its live lease, or row-locked by it right
    # now. The locked ones are only countable when this page was not full (a
    # full page leaves the rest for the next pass anyway).
    skipped = select(func.count()).select_from(PendingObjectDeletion).where(*scope)
    if taken:
        skipped = skipped.where(PendingObjectDeletion.id.not_in(taken))
    if limit is not None and len(taken) >= limit:
        skipped = skipped.where(leased)
    result.skipped_claimed += int((await db.execute(skipped)).scalar_one())

    claims: list[_Claim] = []
    for row_id, prefix, last_error in rows:
        if last_error == CLAIMED:
            # An earlier drain claimed it and never finished: it may have
            # emptied the folder and lost its audit row (finishing transaction
            # failed). This pass records the deletion in its place.
            result.reclaimed += 1
        invoice_id = _invoice_id_of(organization_id, prefix)
        refused = invoice_id is None or await _owned_by_a_live_invoice(db, invoice_id, prefix)
        if refused:
            result.refused += 1
        claims.append(_Claim(row_id=row_id, prefix=prefix, refused=refused))
    if taken:
        await db.execute(
            update(PendingObjectDeletion)
            .where(PendingObjectDeletion.id.in_(taken))
            .values(
                attempts=PendingObjectDeletion.attempts + 1,
                last_error=CLAIMED,
                last_attempt_at=stamp,
            )
        )
    return claims


async def _empty_folders(claims: list[_Claim], result: PurgeResult) -> list[uuid.UUID]:
    """Empty every claimed folder that may go. Storage only: no session is
    touched here. Returns the rows whose folder could not be emptied."""
    failed: list[uuid.UUID] = []
    for claim in claims:
        if claim.refused:
            continue
        try:
            removed = await storage.delete_prefix(claim.prefix)
        except Exception as exc:  # noqa: BLE001 — one folder must not halt the pass
            result.failed += 1
            failed.append(claim.row_id)
            logger.warning("[invoice-objects] folder delete failed: %s", exc.__class__.__name__)
            continue
        result.folders_purged += 1
        result.objects_deleted += removed
    return failed


async def drain_pending(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    row_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    own_transactions: bool = False,
) -> PurgeResult:
    """Empty queued folders; remove each row once its folder is gone.

    ``row_ids`` names the rows a just-committed delete queued (the post-commit
    path); otherwise the oldest ``limit`` rows (the sweep, an erasure). Three
    steps, and only ``db``'s ONE connection is ever used — a dispatcher's or
    the sweep's engine may hold a single connection:

    1. claim the rows and read ownership (:func:`_claim`, which bumps
       ``attempts`` and decides, per folder, whether it may go);
    2. empty the folders — storage only, no statement issued;
    3. delete the emptied and refused rows, record failures, write the audit row.

    ``own_transactions=True`` (the post-commit job, whose session is its own)
    commits after 1 and after 3, so no row lock and no connection is held
    across a storage call, and a concurrent drain sees the claim. Without it
    (the retention sweep and an erasure, inside their own transaction) nothing
    here commits: committing would publish the caller's half-done work early,
    and opening a second session would need a second connection while the
    caller's sits idle in its transaction. The claimed rows then stay
    row-locked until the caller commits; every drain reads them ``SKIP
    LOCKED`` and nothing else updates them, so nobody waits on that lock.

    A failure keeps the row with ``last_error`` (a class name) for the next
    pass. A refused folder — one a live invoice still owns — drops the row and
    keeps every object: the row was wrong, the documents are not.
    """
    result = PurgeResult()
    claims = await _claim(
        db, organization_id=organization_id, row_ids=row_ids, limit=limit, result=result
    )
    if own_transactions:
        await db.commit()
    if not claims:
        return result
    failed = await _empty_folders(claims, result)
    finished = [c.row_id for c in claims if c.row_id not in failed]
    if finished:
        await db.execute(
            delete(PendingObjectDeletion).where(PendingObjectDeletion.id.in_(finished))
        )
    if failed:
        await db.execute(
            update(PendingObjectDeletion)
            .where(PendingObjectDeletion.id.in_(failed))
            .values(last_error="storage_error")
        )
    await _audit(db, organization_id, "invoice_deleted" if row_ids else "retry", result)
    if own_transactions:
        await db.commit()
    return result


async def sweep_orphaned_invoice_objects(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    limit: int | None = None,
    now: datetime | None = None,
    grace: timedelta = ORPHAN_GRACE,
    trigger: str = "orphan_scan",
) -> PurgeResult:
    """Delete invoice folders no live invoice owns, one bounded page per base.

    Lists at most ``limit`` folders under ``{org}/`` and under ``{org}/chat/``
    (one S3 page each, resuming where the last pass stopped), keeps the ones
    that parse as invoice folders, and deletes a folder only when no live
    invoice owns it and its newest object is older than ``grace``. Caller
    commits.
    """
    result = PurgeResult()
    ref_now = now or datetime.now(UTC)
    page = max(1, min(int(limit or settings.retention_batch_size), 1000))
    for base in (f"{organization_id}/", f"{organization_id}/chat/"):
        cursor_key = (organization_id, base)
        try:
            children, cursor = await storage.list_child_prefixes(
                base, cursor=_orphan_cursors.get(cursor_key), limit=page
            )
        except Exception as exc:  # noqa: BLE001 — storage down: count it, next pass retries
            result.failed += 1
            logger.warning("[invoice-objects] orphan listing failed: %s", exc.__class__.__name__)
            continue
        _orphan_cursors[cursor_key] = cursor
        candidates = {
            child: inv_id
            for child in children
            if (inv_id := _invoice_id_of(organization_id, child)) is not None
        }
        if not candidates:
            continue
        live = set(
            (await db.execute(select(Invoice.id).where(Invoice.id.in_(set(candidates.values())))))
            .scalars()
            .all()
        )
        for prefix, inv_id in candidates.items():
            if inv_id in live:
                continue
            try:
                newest = await storage.newest_object_time(prefix)
            except Exception as exc:  # noqa: BLE001 — unseen means untouched
                result.failed += 1
                logger.warning(
                    "[invoice-objects] orphan age read failed: %s", exc.__class__.__name__
                )
                continue
            if newest is not None and newest > ref_now - grace:
                result.skipped_fresh += 1
                continue
            await _purge_folder(db, organization_id, prefix, result)
    await _audit(db, organization_id, trigger, result)
    return result
