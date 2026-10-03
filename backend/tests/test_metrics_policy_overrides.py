import asyncio
from datetime import datetime, timezone

import pytest

from app.core.db import async_session_factory, engine
from app.models import Company, Invoice, PolicyDecision, RecoveryAction, RecoveryCase
from app.models.enums import (
    CompanySegment,
    InvoiceStatus,
    PolicyDecisionResult,
    ProposedBy,
    RecoveryActionStatus,
    RecoveryActionType,
    RecoveryCaseStatus,
)
from app.seed.run import seed
from app.services.metrics import get_policy_override_stats
from app.services.policy_engine import RULE_COOLDOWN_NOT_ELAPSED

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    asyncio.run(seed())
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def _add_action(
    session, case_id, *, seq, recommended: RecoveryActionType, actual: RecoveryActionType, rule: str
) -> None:
    now = datetime.now(timezone.utc)
    action = RecoveryAction(
        recovery_case_id=case_id,
        action_type=actual,
        recommended_action_type=recommended,
        status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI,
        sequence_number=seq,
        executed_at=now,
    )
    session.add(action)
    await session.flush()
    session.add(
        PolicyDecision(
            recovery_action_id=action.id,
            policy_name="deterministic_policy_engine",
            decision=PolicyDecisionResult.APPROVED,
            reason="test fixture",
            rule=rule,
            evaluated_at=now,
        )
    )


async def test_policy_override_stats_on_seeded_data_has_one_flagship_override():
    # Vertex's third action is the flagship: recommended SEND_PAYMENT_LINK,
    # policy forced ESCALATE (see app/seed/run.py's scenario_c_high_risk_escalated).
    async with async_session_factory() as session:
        stats = await get_policy_override_stats(session)

    assert stats.total_evaluated == 9
    assert stats.override_count == 1
    assert stats.override_rate == pytest.approx(1 / 9)
    # by_rule is scoped to genuine overrides only — the other 8 rows are
    # evaluated (counted in total_evaluated) but recommended == actual, so
    # they don't appear here even though their rule (e.g. reminder_approved)
    # fired.
    assert stats.by_rule == {"high_value_overdue_forced_escalate": 1}

    assert len(stats.examples) == 1
    example = stats.examples[0]
    assert example.company_name == "Vertex Infra Solutions"
    assert example.invoice_number == "INV-VERTEX-3010"
    assert example.recommended_action_type == "SEND_PAYMENT_LINK"
    assert example.action_type == "ESCALATE"
    assert example.rule == "high_value_overdue_forced_escalate"


async def test_policy_override_stats_ignore_rows_with_no_recorded_recommendation():
    # A legacy-shaped row (recommended_action_type left NULL, as pre-migration
    # rows would be) must not count toward total_evaluated or override_count —
    # counting it as "not overridden" would understate the real rate.
    async with async_session_factory() as session:
        company = Company(name="Metrics Test Co", industry="Testing", segment=CompanySegment.SMB)
        session.add(company)
        await session.flush()

        invoice = Invoice(
            company_id=company.id,
            invoice_number="INV-METRICSTEST-0001",
            amount_total="10000.00",
            amount_paid="0.00",
            issue_date=datetime.now(timezone.utc).date(),
            due_date=datetime.now(timezone.utc).date(),
            status=InvoiceStatus.OVERDUE,
        )
        session.add(invoice)
        await session.flush()

        case = RecoveryCase(
            invoice_id=invoice.id,
            company_id=company.id,
            status=RecoveryCaseStatus.OPEN,
            revenue_at_risk="10000.00",
        )
        session.add(case)
        await session.flush()

        # No recommended_action_type set — simulates a pre-migration row.
        legacy_action = RecoveryAction(
            recovery_case_id=case.id,
            action_type=RecoveryActionType.SEND_EMAIL,
            status=RecoveryActionStatus.EXECUTED,
            proposed_by=ProposedBy.AI,
            sequence_number=1,
        )
        session.add(legacy_action)
        await session.flush()
        session.add(
            PolicyDecision(
                recovery_action_id=legacy_action.id,
                policy_name="deterministic_policy_engine",
                decision=PolicyDecisionResult.APPROVED,
                reason="legacy row, no rule recorded",
                evaluated_at=datetime.now(timezone.utc),
            )
        )

        before = await get_policy_override_stats(session)

        await _add_action(
            session,
            case.id,
            seq=2,
            recommended=RecoveryActionType.SEND_EMAIL,
            actual=RecoveryActionType.WAIT,
            rule=RULE_COOLDOWN_NOT_ELAPSED,
        )
        await session.commit()

        after = await get_policy_override_stats(session)

    # The legacy row (no recommended_action_type) contributes nothing.
    assert after.total_evaluated == before.total_evaluated + 1
    assert after.override_count == before.override_count + 1
    assert after.by_rule.get(RULE_COOLDOWN_NOT_ELAPSED) == 1
    assert len(after.examples) == len(before.examples) + 1
    assert after.examples[0].company_name == "Metrics Test Co"
