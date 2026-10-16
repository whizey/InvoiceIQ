"""Read models and bookkeeping owned by the event consumer."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class ProcessedEvent(Base):
    """One row per (consumer, event). Inserting it is what makes redelivery harmless."""
    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(60), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CaseRiskLatest(Base):
    """Latest risk score per recovery case; the high-risk table is a filter over it."""
    __tablename__ = "case_risk_latest"
    __table_args__ = (Index("ix_case_risk_level", "risk_level"),)

    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_score: Mapped[float | None] = mapped_column(Float)
    recovery_probability: Mapped[float | None] = mapped_column(Float)
    drivers: Mapped[list | dict | None] = mapped_column(JSONB)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalAlert(Base, CreatedAtMixin):
    """In-app alert raised when an action needs a human decision. Sends nothing externally."""
    __tablename__ = "approval_alerts"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    action_id: Mapped[str | None] = mapped_column(String(64))
    action_type: Mapped[str | None] = mapped_column(String(60))
    reason: Mapped[str | None] = mapped_column(Text)


class DeadLetterEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Events that failed repeatedly or could not be understood; kept for inspection and replay."""
    __tablename__ = "dead_letter_events"
    __table_args__ = (UniqueConstraint("consumer", "event_id", name="uq_dead_letter_event"),)

    consumer: Mapped[str] = mapped_column(String(60), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict | list | None] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
