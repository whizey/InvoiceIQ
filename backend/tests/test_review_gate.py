"""The human-approval gate: a held action must not execute until approved."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agents.graph import run_recovery_cycle
from app.core.db import async_session_factory, engine
from app.core.time import utc_today
from app.events import topics
from app.models import AuditLog, Company, Invoice, RecoveryAction
from app.models.enums import (
    AuditActor,
    CompanySegment,
    InvoiceStatus,
    RecoveryActionStatus,
)
from app.seed.run import seed
from app.services import review_gate
from app.services.risk_engine import run_detection

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    async def prepare():
        try:
            await seed()
        finally:
            await engine.dispose()

    asyncio.run(prepare())
    yield
    asyncio.run(engine.dispose())


async def _high_value_case(tag: str):
    async with async_session_factory() as session:
        company = Company(name=f"Gate Test {tag}", industry="Testing", segment=CompanySegment.MID_MARKET)
        session.add(company)
        await session.flush()
        due = utc_today() - timedelta(days=5)
        invoice = Invoice(
            company_id=company.id,
            invoice_number=f"INV-GATE-{tag}",
            amount_total=Decimal("1200000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.SENT,
        )
        session.add(invoice)
        await session.commit()
    async with async_session_factory() as session:
        result = await run_detection(session)
    return next(c for c in result.cases_created if c.invoice_id == invoice.id)


async def _actions(case_id):
    async with async_session_factory() as session:
        return (
            await session.execute(select(RecoveryAction).where(RecoveryAction.recovery_case_id == case_id))
        ).scalars().all()


async def test_high_value_reminder_is_held_and_not_executed():
    case = await _high_value_case("A")
    async with async_session_factory() as session:
        state = await run_recovery_cycle(session, case.id)

    assert state["policy_decision"] == "REQUIRES_HUMAN_REVIEW"
    assert "case_status" not in state  # workflow stopped before execute_action
    actions = await _actions(case.id)
    assert len(actions) == 1
    assert actions[0].status == RecoveryActionStatus.PENDING_APPROVAL
    assert actions[0].executed_at is None


async def test_second_cycle_does_not_stack_another_action():
    case = await _high_value_case("B")
    async with async_session_factory() as session:
        await run_recovery_cycle(session, case.id)
    async with async_session_factory() as session:
        state = await run_recovery_cycle(session, case.id)

    assert state.get("awaiting_review") is True
    assert len(await _actions(case.id)) == 1


async def test_approve_executes_once_and_audits_the_human():
    case = await _high_value_case("C")
    async with async_session_factory() as session:
        await run_recovery_cycle(session, case.id)
    action_id = (await _actions(case.id))[0].id

    async with async_session_factory() as session:
        result = await review_gate.approve(session, action_id, "riya", "ok to send")
        await session.commit()
    assert result["decision"] == "APPROVED"

    action = (await _actions(case.id))[0]
    assert action.status == RecoveryActionStatus.EXECUTED
    assert action.executed_at is not None

    async with async_session_factory() as session:
        logs = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.recovery_case_id == case.id, AuditLog.event_type == "REVIEW_APPROVED"
                )
            )
        ).scalars().all()
    assert len(logs) == 1 and logs[0].actor == AuditActor.HUMAN

    async with async_session_factory() as session:
        with pytest.raises(review_gate.ReviewError) as err:
            await review_gate.approve(session, action_id, "riya", None)
    assert err.value.status_code == 409


async def test_reject_never_executes():
    case = await _high_value_case("D")
    async with async_session_factory() as session:
        await run_recovery_cycle(session, case.id)
    action_id = (await _actions(case.id))[0].id

    async with async_session_factory() as session:
        result = await review_gate.reject(session, action_id, "riya", "customer disputed")
        await session.commit()
    assert result["decision"] == "REJECTED"

    action = (await _actions(case.id))[0]
    assert action.status == RecoveryActionStatus.POLICY_REJECTED
    assert action.executed_at is None


async def test_unknown_action_is_404():
    from uuid import uuid4

    async with async_session_factory() as session:
        with pytest.raises(review_gate.ReviewError) as err:
            await review_gate.approve(session, uuid4(), "riya", None)
    assert err.value.status_code == 404


async def test_risk_scored_audit_row_carries_shap_drivers(monkeypatch):
    drivers = [
        {
            "feature": "historical_late_rate",
            "label": "Historical late rate",
            "value": 0.4,
            "shap_value": 0.12,
            "direction": "increases_late_payment_probability",
        },
        {
            "feature": "invoice_amount",
            "label": "Invoice amount",
            "value": 1200000,
            "shap_value": 0.08,
            "direction": "increases_late_payment_probability",
        },
        {
            "feature": "payment_term",
            "label": "Payment term",
            "value": 30,
            "shap_value": -0.04,
            "direction": "decreases_late_payment_probability",
        },
    ]

    async def controlled_scoring(session, invoice):
        invoice.late_payment_probability = Decimal("0.700000")
        invoice.payment_risk_model = "xgboost"
        invoice.payment_risk_model_version = "test-fixture"
        invoice.payment_risk_drivers = drivers
        await session.flush()
        return True

    monkeypatch.setattr(
        "app.agents.graph.ensure_payment_risk_score",
        controlled_scoring,
    )

    case = await _high_value_case("E")

    async with async_session_factory() as session:
        state = await run_recovery_cycle(session, case.id)

    assert state["risk_drivers"] == drivers
    assert state["recovery_probability"] is None

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.recovery_case_id == case.id,
                    AuditLog.event_type == "RISK_SCORED",
                )
            )
        ).scalars().all()

    assert any(
        "Top SHAP drivers" in row.description
        and all(
            driver["label"] in row.description
            for driver in drivers
        )
        for row in rows
    )


async def test_new_kafka_topics_are_registered():
    for t in (topics.RISK_SCORED, topics.ACTION_PROPOSED, topics.REVIEW_REQUESTED, topics.REVIEW_RESOLVED):
        assert t in topics.ALL_TOPICS
