"""Provenance for a reconciled public-source import, independent of live model serving."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DatasetImport(Base):
    __tablename__ = "dataset_imports"

    source: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_total: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OfflineEvaluation(Base):
    __tablename__ = "offline_evaluations"
    source: Mapped[str] = mapped_column(String(250), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_family: Mapped[str] = mapped_column(String(100), nullable=False)
    evaluation_context: Mapped[str] = mapped_column(String(100), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
