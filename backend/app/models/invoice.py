import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import InvoiceStatus, PaymentEventType, PaymentMethod, PaymentStatus


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoices_due_date_status", "due_date", "status"),)

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    amount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    posting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    payment_term: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    payment_method_description: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    late_payment_probability: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 6), nullable=True
    )
    payment_risk_model: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    payment_risk_scored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payment_risk_drivers: Mapped[list[dict] | None] = mapped_column(
        JSONB, nullable=True
    )
    payment_risk_features: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )

    status: Mapped[InvoiceStatus] = mapped_column(
        SAEnum(InvoiceStatus, name="invoice_status"), nullable=False, default=InvoiceStatus.SENT
    )

    company: Mapped["Company"] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    payment_events: Mapped[list["PaymentEvent"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    recovery_case: Mapped["RecoveryCase | None"] = relationship(
        back_populates="invoice", uselist=False
    )

    @property
    def amount_outstanding(self) -> Decimal:
        return self.amount_total - self.amount_paid


class Payment(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "payments"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    payment_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        SAEnum(PaymentMethod, name="payment_method"), nullable=False
    )
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status"), nullable=False, default=PaymentStatus.SUCCESS
    )
    reference_note: Mapped[str | None] = mapped_column(Text)

    invoice: Mapped["Invoice"] = relationship(back_populates="payments")


class PaymentEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "payment_events"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[PaymentEventType] = mapped_column(
        SAEnum(PaymentEventType, name="payment_event_type"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    invoice: Mapped["Invoice"] = relationship(back_populates="payment_events")
