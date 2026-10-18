"""Durable inference attempts and database-maintained financial snapshots."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import BigInteger, Date, DateTime, Float, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, UUIDPrimaryKeyMixin


class AnalyticsHistoryState(Base):
    __tablename__ = "analytics_history_state"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvoiceStateSnapshot(Base):
    __tablename__ = "invoice_state_snapshots"
    __table_args__ = (Index("ix_invoice_snapshot_asof", "company_id", "observed_at", "invoice_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False)
    company_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)


class CaseStateSnapshot(Base):
    __tablename__ = "case_state_snapshots"
    __table_args__ = (Index("ix_case_snapshot_asof", "company_id", "observed_at", "case_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False)
    invoice_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    company_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)


class InferenceRecord(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "inference_records"
    __table_args__ = (Index("ix_inference_scope_time", "company_id", "started_at"),
                     Index("ix_inference_workspace_time", "workspace_id", "started_at"),)
    # No invoice FK: a failed/rolled-back ingestion must retain its inference attempt.
    invoice_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    company_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    workspace_id: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str | None] = mapped_column(String(3))
    model_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(40))
    probability: Mapped[float | None] = mapped_column(Float)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
