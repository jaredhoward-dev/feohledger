"""Object-storage folders queued for deletion — ``pending_object_deletions``.

A transactional outbox for the bucket (decisions §592). Deleting an invoice
removes its rows in one transaction, but its documents live in object storage,
which no database transaction can reach. So the delete transaction writes one
row here per folder the invoice owned (``{org}/{invoice}/`` and
``{org}/chat/{invoice}/``), and the objects are removed only after that
transaction commits — by a ``post_commit`` job straight away, and by the
retention sweep for anything that job could not finish. A row leaves this table
only when its folder has been emptied, so a storage outage, a crashed worker or
a restart between the commit and the delete leaves a durable to-do, never a
silently orphaned document.

``prefix`` is a folder (ends in ``/``) under the owning organisation's id,
holding ids only. ``last_error`` is an exception CLASS name, never a message: a
storage error's text embeds the key. Nothing here is personal data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin

#: The only owner kind today. A column rather than an assumption so a later
#: owner (a deleted contract, say) joins the same outbox instead of a second one.
OWNER_INVOICE = "invoice"


class PendingObjectDeletion(TenantMixin, Base):
    __tablename__ = "pending_object_deletions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prefix: Mapped[str] = mapped_column(String(512), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(30), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(String(100))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
