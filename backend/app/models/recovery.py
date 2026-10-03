import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    AgentDecisionStage,
    AuditActor,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationStatus,
    PolicyDecisionResult,
    ProposedBy,
    PromiseToPayStatus,
    RecoveryActionStatus,
    RecoveryActionType,
    RecoveryCaseStatus,
    RiskLevel,
)


class RecoveryCase(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "recovery_cases"
    __table_args__ = (Index("ix_recovery_cases_status", "status"),)

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[RecoveryCaseStatus] = mapped_column(
        SAEnum(RecoveryCaseStatus, name="recovery_case_status"),
        nullable=False,
        default=RecoveryCaseStatus.OPEN,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revenue_at_risk: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    recovered_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    risk_level: Mapped[RiskLevel | None] = mapped_column(SAEnum(RiskLevel, name="risk_level"))
    recovery_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    recovery_window_deadline: Mapped[date | None] = mapped_column(Date)

    invoice: Mapped["Invoice"] = relationship(back_populates="recovery_case")
    actions: Mapped[list["RecoveryAction"]] = relationship(
        back_populates="recovery_case",
        cascade="all, delete-orphan",
        order_by="RecoveryAction.sequence_number",
    )
    # These three carry an explicit order_by for the same reason `actions`
    # and `audit_logs` above do: without it SQLAlchemy emits no ORDER BY,
    # Postgres is free to return rows in any order, and the consumers assume
    # chronological. The case detail page does
    # `[...agent_decisions].reverse().find(...)` to pull the latest
    # diagnosis, which silently shows a stale cycle once row updates or a
    # vacuum change the physical order, and renders communication_logs in
    # relationship order despite sent_at being available. Arbitrary order is
    # not the same as insertion order.
    agent_decisions: Mapped[list["AgentDecision"]] = relationship(
        back_populates="recovery_case",
        cascade="all, delete-orphan",
        order_by="AgentDecision.created_at",
    )
    promises_to_pay: Mapped[list["PromiseToPay"]] = relationship(
        back_populates="recovery_case",
        cascade="all, delete-orphan",
        order_by="PromiseToPay.created_at",
    )
    communication_logs: Mapped[list["CommunicationLog"]] = relationship(
        back_populates="recovery_case",
        cascade="all, delete-orphan",
        order_by="CommunicationLog.sent_at",
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="recovery_case",
        cascade="all, delete-orphan",
        order_by="AuditLog.occurred_at",
    )


class RecoveryAction(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "recovery_actions"

    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[RecoveryActionType] = mapped_column(
        SAEnum(RecoveryActionType, name="recovery_action_type"), nullable=False
    )
    status: Mapped[RecoveryActionStatus] = mapped_column(
        SAEnum(RecoveryActionStatus, name="recovery_action_status"),
        nullable=False,
        default=RecoveryActionStatus.PROPOSED,
    )
    proposed_by: Mapped[ProposedBy] = mapped_column(
        SAEnum(ProposedBy, name="proposed_by"), nullable=False, default=ProposedBy.AI
    )
    # What the LLM/rule-based agent actually recommended before the policy
    # engine ran, captured verbatim so "AI suggested X, policy did Y" is a
    # direct column comparison against action_type rather than an inferred
    # pairing. Nullable: rows created before this column existed have none.
    recommended_action_type: Mapped[RecoveryActionType | None] = mapped_column(
        SAEnum(RecoveryActionType, name="recovery_action_type")
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recovery_case: Mapped["RecoveryCase"] = relationship(back_populates="actions")
    policy_decisions: Mapped[list["PolicyDecision"]] = relationship(
        back_populates="recovery_action", cascade="all, delete-orphan"
    )


class AgentDecision(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "agent_decisions"

    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage: Mapped[AgentDecisionStage] = mapped_column(
        SAEnum(AgentDecisionStage, name="agent_decision_stage"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    recovery_case: Mapped["RecoveryCase"] = relationship(back_populates="agent_decisions")


class PromiseToPay(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "promise_to_pay"
    __table_args__ = (Index("ix_promise_to_pay_status_date", "status", "promised_date"),)

    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    promised_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    promised_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[PromiseToPayStatus] = mapped_column(
        SAEnum(PromiseToPayStatus, name="promise_to_pay_status"),
        nullable=False,
        default=PromiseToPayStatus.PENDING,
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recovery_case: Mapped["RecoveryCase"] = relationship(back_populates="promises_to_pay")


class CommunicationLog(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "communication_logs"

    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    channel: Mapped[CommunicationChannel] = mapped_column(
        SAEnum(CommunicationChannel, name="communication_channel"),
        nullable=False,
        default=CommunicationChannel.EMAIL,
    )
    direction: Mapped[CommunicationDirection] = mapped_column(
        SAEnum(CommunicationDirection, name="communication_direction"),
        nullable=False,
        default=CommunicationDirection.OUTBOUND,
    )
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CommunicationStatus] = mapped_column(
        SAEnum(CommunicationStatus, name="communication_status"),
        nullable=False,
        default=CommunicationStatus.SIMULATED,
    )
    # The actual delivery address for a real (non-mock) send — e.g. always
    # settings.demo_notify_email for the real-reminder-email feature, never
    # the seeded contact's @example.com. NULL for the existing always-mock
    # tools, which never had a real destination to record.
    recipient_email: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recovery_case: Mapped["RecoveryCase"] = relationship(back_populates="communication_logs")


class PolicyDecision(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "policy_decisions"

    recovery_action_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    policy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[PolicyDecisionResult] = mapped_column(
        SAEnum(PolicyDecisionResult, name="policy_decision_result"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Machine-readable branch tag from evaluate_policy() (see
    # app/services/policy_engine.py's RULE_* constants) — `reason` has
    # numbers baked into the string and can't be grouped for stats; this can.
    # Nullable: rows created before this column existed have none.
    rule: Mapped[str | None] = mapped_column(String(100))
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    recovery_action: Mapped["RecoveryAction"] = relationship(back_populates="policy_decisions")


class AuditLog(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_case_occurred", "recovery_case_id", "occurred_at"),)

    recovery_case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recovery_cases.id", ondelete="CASCADE")
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[AuditActor] = mapped_column(SAEnum(AuditActor, name="audit_actor"), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    recovery_case: Mapped["RecoveryCase | None"] = relationship(back_populates="audit_logs")
