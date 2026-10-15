from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

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
from app.schemas.invoice import InvoiceOut


class PolicyDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    policy_name: str
    decision: PolicyDecisionResult
    reason: str
    rule: str | None
    evaluated_at: datetime


class RecoveryActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    action_type: RecoveryActionType
    recommended_action_type: RecoveryActionType | None
    status: RecoveryActionStatus
    proposed_by: ProposedBy
    sequence_number: int
    executed_at: datetime | None
    result: dict | None
    created_at: datetime
    policy_decisions: list[PolicyDecisionOut] = []


class AgentDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stage: AgentDecisionStage
    model_name: str
    output: dict
    rationale: str
    created_at: datetime


class PromiseToPayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    promised_amount: Decimal
    promised_date: date
    status: PromiseToPayStatus
    fulfilled_at: datetime | None


class CommunicationLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel: CommunicationChannel
    direction: CommunicationDirection
    subject: str | None
    body: str | None
    status: CommunicationStatus
    recipient_email: str | None
    sent_at: datetime | None


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    entity_type: str
    entity_id: UUID
    event_type: str
    actor: AuditActor
    description: str
    occurred_at: datetime


class RiskFeatureContributionOut(BaseModel):
    """SHAP contribution to the late-payment model output."""

    feature: str
    label: str
    value: float | str | None = None
    shap_value: float
    direction: Literal[
        "increases_late_payment_probability",
        "decreases_late_payment_probability",
    ]


class RiskExplanationOut(BaseModel):
    """Posting-time B2B risk, not overdue recovery probability."""

    prediction_target: Literal["late_payment_weekend_adjusted"]
    experimental: bool
    model_name: str | None
    model_version: str | None
    scored_at: datetime | None
    late_payment_probability: float | None
    risk_score: float | None
    risk_level: RiskLevel | None
    contributions: list[RiskFeatureContributionOut]


class RecoveryCaseListItemOut(BaseModel):
    """Shape matches the planned dashboard case table."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_name: str
    invoice_number: str
    amount_total: Decimal
    days_overdue: int
    status: RecoveryCaseStatus
    risk_level: RiskLevel | None
    recovery_probability: Decimal | None
    current_action: RecoveryActionType | None
    recovered_amount: Decimal


class DetectionSummaryOut(BaseModel):
    invoices_marked_overdue: int
    cases_created: int
    case_ids: list[UUID]
    promises_fulfilled: int
    promises_broken: int


class SendReminderEmailOut(BaseModel):
    """Result of a human-triggered real reminder email — always delivered to
    (or simulated for) settings.demo_notify_email, never a seeded contact."""

    status: Literal["SENT", "SIMULATED", "REJECTED", "PENDING_APPROVAL"]
    to: str | None
    sent_at: datetime | None
    policy_decision: PolicyDecisionResult
    reason: str


class RecoveryCaseDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: RecoveryCaseStatus
    opened_at: datetime
    closed_at: datetime | None
    revenue_at_risk: Decimal
    recovered_amount: Decimal
    risk_score: Decimal | None
    risk_level: RiskLevel | None
    recovery_probability: Decimal | None
    recovery_window_deadline: date | None
    invoice: InvoiceOut
    actions: list[RecoveryActionOut]
    agent_decisions: list[AgentDecisionOut]
    promises_to_pay: list[PromiseToPayOut]
    communication_logs: list[CommunicationLogOut]
    audit_logs: list[AuditLogOut]
