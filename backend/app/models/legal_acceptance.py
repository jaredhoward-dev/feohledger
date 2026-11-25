"""Who accepted which version of our Terms of Service and Privacy Policy, and when.

Control plane, append-only. One row per (user, document, version) accepted: the
evidence that a customer agreed to the terms `/legal/terms` says are accepted
"by clicking an accept" control, which the Xero developer terms also require
(`docs/decisions.md` §495). Rows are only ever inserted, never updated; a newly
published version is a new row, so the history of what each user agreed to
survives every republish.

Written by ``services/legal_acceptance.record_acceptances`` from exactly three
places: signup completion (the acceptance was given on the signup form and
carried on the verification row), the set-password / reset-password step, and
``POST /api/auth/legal-acceptance`` (the blocking re-prompt after a version
changes).

No foreign keys, deliberately, like ``workspace_closures``: the row is evidence
of a contract being formed and must outlive the user row an erasure redacts or
a workspace deletion removes. No email, name, IP address or user agent — the
user id and organisation id correlate the row with the control plane and with
the tenant's shipped auth audit (``auth.legal.accepted``, which carries the IP
the way every auth audit row does), and nothing here identifies anyone on its
own once the user row is gone.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LegalAcceptance(Base):
    __tablename__ = "legal_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "document", "version", name="uq_legal_acceptances_user_doc_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: ``services/legal_acceptance.LEGAL_DOCUMENTS`` key: ``terms`` or ``privacy``.
    document: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The document's version as published — its effective date, ``YYYY-MM-DD``.
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Which control the acceptance came through: ``signup``, ``set_password`` or
    #: ``reprompt``. Evidence of HOW it was given, not just that it was.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
