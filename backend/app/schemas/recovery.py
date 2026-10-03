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
    """One local SHAP contribution for a recovery-risk prediction."""

    model_config = ConfigDict(from_attributes=True)

    feature: str
    label: str
    value: float
    shap_value: float
    direction: Literal[
        "increases_recovery_probability",
        "decreases_recovery_probability",
    ]


class RiskExplanationOut(BaseModel):
    """Local XGBoost + SHAP explanation for one recovery case."""

    model_config = ConfigDict(from_attributes=True)

    risk_score: float
    risk_level: RiskLevel
    recovery_probability: float
    base_value: float
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

    status: Literal["SENT", "SIMULATED", "REJECTED"]
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
