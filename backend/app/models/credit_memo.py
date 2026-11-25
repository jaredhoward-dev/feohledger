import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin

#: Every status a memo can hold. `applied` is all-or-nothing — one memo credits
#: its whole amount to exactly one invoice in one transaction — so there is no
#: "partially applied" state to represent.
CREDIT_MEMO_STATUSES: tuple[str, ...] = ("open", "applied", "void")


class CreditMemo(Base, EntityMixin, TimestampMixin):
    """Vendor-issued credit reducing what's owed.

    Lifecycle: ``open`` (issued, unapplied) → ``applied`` (linked to an
    invoice; reduces the payable) → ``void`` (rescinded). Applied
    credits are immutable for audit purposes. Only an ``open`` memo that has
    never been applied is editable (``PATCH /api/credit-memos/{id}``) — until
    then it moves no money: only ``applied`` rows are netted off a payable.
    """

    __tablename__ = "credit_memos"
    # One live memo per discount offer: an offer's savings is one deduction, so
    # a second linked memo would take it twice. Void frees the offer again.
    # Migration 0145 builds the same index for tenants that predate it.
    __table_args__ = (
        Index(
            "uq_credit_memos_discount_offer_live",
            "discount_offer_id",
            unique=True,
            postgresql_where=text("discount_offer_id IS NOT NULL AND status <> 'void'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    memo_number: Mapped[str] = mapped_column(String(100), nullable=False)
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True
    )
    # Set when applied to an invoice. Null while status='open'.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    issued_date: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, applied, void
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[str | None] = mapped_column(String(255))
    # Set when this memo IS an accepted early-payment discount taken as a
    # credit (decisions §520, migration 0145) — only by `POST /api/credit-memos`
    # with `discount_offer_id`, which creates the memo applied, for exactly the
    # offer's savings, on the offer's own invoice. It is what tells the payment
    # path the discount was already taken (`discount_capture.
    # applicable_discounts`) and what the settlement captures when the payment
    # booked no discount of its own. NULL means an ordinary credit (a return,
    # a price correction) whatever its amount: the old amount match read a
    # return credit equal to the savings as the discount. Rows from before 0145
    # are all NULL; nothing is inferred for them.
    discount_offer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discount_offers.id", ondelete="SET NULL"),
        index=True,
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
