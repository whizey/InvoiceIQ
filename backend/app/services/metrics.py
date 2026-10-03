"""Aggregate revenue-recovery metrics for the dashboard.

All numbers come from real aggregate queries over recovery_cases/invoices —
nothing here is precomputed or cached at write time, since the dataset is
small enough that recomputing on read is simpler and can't drift.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company, Invoice, PolicyDecision, RecoveryAction, RecoveryCase
from app.models.enums import RecoveryCaseStatus

ACTIVE_STATUSES = [RecoveryCaseStatus.OPEN, RecoveryCaseStatus.MONITORING, RecoveryCaseStatus.ESCALATED]


@dataclass
class DashboardMetrics:
    total_revenue_at_risk: Decimal
    total_revenue_recovered: Decimal
    recovery_rate: float | None
    active_cases: int
    escalated_cases: int
    average_days_overdue: float | None
    cases_by_risk_level: dict[str, int]


@dataclass
class PolicyOverrideExample:
    case_id: str
    company_name: str
    invoice_number: str
    recommended_action_type: str
    action_type: str
    rule: str | None


@dataclass
class PolicyOverrideStats:
    total_evaluated: int
    override_count: int
    override_rate: float | None
    by_rule: dict[str, int]
    examples: list[PolicyOverrideExample]


async def get_dashboard_metrics(session: AsyncSession) -> DashboardMetrics:
    total_at_risk_result = await session.execute(
        select(func.coalesce(func.sum(RecoveryCase.revenue_at_risk), 0)).where(
            RecoveryCase.status.in_(ACTIVE_STATUSES)
        )
    )
    total_revenue_at_risk = total_at_risk_result.scalar_one()

    total_recovered_result = await session.execute(
        select(func.coalesce(func.sum(RecoveryCase.recovered_amount), 0))
    )
    total_revenue_recovered = total_recovered_result.scalar_one()

    # Of the revenue that has ever been at risk (currently at risk + already
    # recovered), what fraction has actually been recovered so far.
    denominator = total_revenue_at_risk + total_revenue_recovered
    recovery_rate = float(total_revenue_recovered / denominator) if denominator else None

    active_cases_result = await session.execute(
        select(func.count(RecoveryCase.id)).where(RecoveryCase.status.in_(ACTIVE_STATUSES))
    )
    active_cases = active_cases_result.scalar_one()

    escalated_cases_result = await session.execute(
        select(func.count(RecoveryCase.id)).where(RecoveryCase.status == RecoveryCaseStatus.ESCALATED)
    )
    escalated_cases = escalated_cases_result.scalar_one()

    avg_days_result = await session.execute(
        select(func.avg(func.current_date() - Invoice.due_date))
        .join(RecoveryCase, RecoveryCase.invoice_id == Invoice.id)
        .where(RecoveryCase.status.in_(ACTIVE_STATUSES))
    )
    avg_days_raw = avg_days_result.scalar_one()
    average_days_overdue = float(avg_days_raw) if avg_days_raw is not None else None

    risk_level_result = await session.execute(
        select(RecoveryCase.risk_level, func.count(RecoveryCase.id)).group_by(RecoveryCase.risk_level)
    )
    cases_by_risk_level = {
        (level.value if level else "UNSCORED"): count for level, count in risk_level_result.all()
    }

    return DashboardMetrics(
        total_revenue_at_risk=total_revenue_at_risk,
        total_revenue_recovered=total_revenue_recovered,
        recovery_rate=recovery_rate,
        active_cases=active_cases,
        escalated_cases=escalated_cases,
        average_days_overdue=average_days_overdue,
        cases_by_risk_level=cases_by_risk_level,
    )


async def get_policy_override_stats(session: AsyncSession) -> PolicyOverrideStats:
    """AI-vs-policy divergence: how often the deterministic policy engine's
    final_action differs from what was recommended, and by which rule.

    Only counts actions with a recorded recommended_action_type — rows
    created before that column existed (see the policy-eval-columns
    migration) have it as NULL and are excluded rather than treated as
    "not overridden", since that would be undercounting, not a fact.

    by_rule is scoped to genuine overrides only (recommended != actual) —
    grouping every evaluated action by rule would bury the divergence signal
    under the (usually much larger) count of reminders approved as-is.
    """
    total_result = await session.execute(
        select(func.count(RecoveryAction.id)).where(RecoveryAction.recommended_action_type.is_not(None))
    )
    total_evaluated = total_result.scalar_one()

    is_override = RecoveryAction.recommended_action_type != RecoveryAction.action_type

    override_result = await session.execute(
        select(func.count(RecoveryAction.id)).where(
            RecoveryAction.recommended_action_type.is_not(None), is_override
        )
    )
    override_count = override_result.scalar_one()

    override_rate = (override_count / total_evaluated) if total_evaluated else None

    by_rule_result = await session.execute(
        select(PolicyDecision.rule, func.count(PolicyDecision.id))
        .join(RecoveryAction, RecoveryAction.id == PolicyDecision.recovery_action_id)
        .where(RecoveryAction.recommended_action_type.is_not(None), PolicyDecision.rule.is_not(None), is_override)
        .group_by(PolicyDecision.rule)
    )
    by_rule = {rule: count for rule, count in by_rule_result.all()}

    # A few concrete, real examples for the dashboard's "Example: ..." line —
    # deliberately not hardcoded to any specific seeded case, so this stays
    # honest as live Gemini/scheduler cycles produce their own overrides.
    examples_result = await session.execute(
        select(
            RecoveryCase.id,
            Company.name,
            Invoice.invoice_number,
            RecoveryAction.recommended_action_type,
            RecoveryAction.action_type,
            PolicyDecision.rule,
        )
        .join(RecoveryCase, RecoveryCase.id == RecoveryAction.recovery_case_id)
        .join(Invoice, Invoice.id == RecoveryCase.invoice_id)
        .join(Company, Company.id == RecoveryCase.company_id)
        .join(PolicyDecision, PolicyDecision.recovery_action_id == RecoveryAction.id)
        .where(RecoveryAction.recommended_action_type.is_not(None), is_override)
        .order_by(RecoveryAction.created_at.desc())
        .limit(3)
    )
    examples = [
        PolicyOverrideExample(
            case_id=str(case_id),
            company_name=company_name,
            invoice_number=invoice_number,
            recommended_action_type=recommended.value,
            action_type=actual.value,
            rule=rule,
        )
        for case_id, company_name, invoice_number, recommended, actual, rule in examples_result.all()
    ]

    return PolicyOverrideStats(
        total_evaluated=total_evaluated,
        override_count=override_count,
        override_rate=override_rate,
        by_rule=by_rule,
        examples=examples,
    )
