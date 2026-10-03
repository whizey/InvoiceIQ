from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import RecoveryActionType


class DashboardMetricsOut(BaseModel):
    total_revenue_at_risk: Decimal
    total_revenue_recovered: Decimal
    recovery_rate: float | None
    active_cases: int
    escalated_cases: int
    average_days_overdue: float | None
    cases_by_risk_level: dict[str, int]


class PolicyOverrideExampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    case_id: UUID
    company_name: str
    invoice_number: str
    recommended_action_type: RecoveryActionType
    action_type: RecoveryActionType
    rule: str | None


class PolicyOverrideStatsOut(BaseModel):
    """AI-vs-policy divergence: how often the recommended action differs from
    what the deterministic policy engine actually let run, and which rule
    caused each divergence. See app/services/policy_engine.py's RULE_*
    constants and app/services/metrics.get_policy_override_stats()."""

    total_evaluated: int
    override_count: int
    override_rate: float | None
    by_rule: dict[str, int]
    examples: list[PolicyOverrideExampleOut]
