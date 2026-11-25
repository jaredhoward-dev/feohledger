"""Which version of our Privacy Policy a supplier-portal user acknowledged, and when.

Tenant DB, append-only (`docs/decisions.md` §499). The supplier-portal twin of
the control-plane ``legal_acceptances`` (§495): a ``VendorUser`` lives in the
tenant database and has no control-plane id, so a row keyed by an employee
``user_id`` cannot name them.

Only the Privacy Policy is recorded. The Terms of Service are a contract with
the customer organisation; a Supplier User "is not our customer" and the
customer answers for them (`/legal/terms` §2, §3.4), so a supplier is never
asked to accept the Terms (§499). ``document`` is still a column so a future
portal-terms document (tracked in ``docs/followups.md``) is a new value, not a
new table.

Written by ``services/portal_legal_acceptance.record_acknowledgement`` from one
place: redeeming a portal set-password link for an account with no password
yet (``POST /api/portal/auth/reset-password``).

No foreign key, deliberately, like ``legal_acceptances``: the row must survive
a DSAR erasure of the vendor user (it is kept, §499) and a portal-user delete.
No email, name, IP or user agent. That does NOT make the evidence anonymous
after an erasure: the row keeps ``vendor_user_id``, and the tenant's
``portal.legal.accepted`` audit row for the same event (like every portal auth
event) keeps ``actor_id`` and the client IP in ``details``, and erasure leaves
the audit trail as it is. Whether erasure should null that IP is an open
decision for the CISO (``docs/followups.md``).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class VendorUserLegalAcceptance(Base):
    __tablename__ = "vendor_user_legal_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "vendor_user_id",
            "document",
            "version",
            name="uq_vendor_user_legal_acceptances_doc_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The ``vendor_users.id`` who acknowledged. Leads the unique constraint,
    #: whose index serves the per-user lookup, so no separate index.
    vendor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: ``services/portal_legal_acceptance.PORTAL_LEGAL_DOCUMENTS`` key: ``privacy``.
    document: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The document's version as published — its effective date, ``YYYY-MM-DD``.
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Which control it came through: ``set_password`` (the only one today).
    source: Mapped[str] = mapped_column(String(32), nullable=False)
