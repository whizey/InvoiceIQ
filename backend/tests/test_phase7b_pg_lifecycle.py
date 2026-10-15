"""Phase 7B: transactional PostgreSQL recovery integration tests.

Exercises actual recovery workflows using an isolated test database,
local deterministic reasoning, and simulated actions.
"""

import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents import graph, llm_client
from app.agents.graph import run_recovery_cycle
from app.core.time import utc_today
from app.models import (
    AgentDecision, AuditLog, CommunicationLog, Company,
    Invoice, PolicyDecision, RecoveryAction, RecoveryCase,
)
from app.models.enums import (
    CompanySegment, InvoiceStatus, PolicyDecisionResult,
    RecoveryActionStatus, RecoveryCaseStatus,
)
from app.services import review_gate
from scripts.test_database_safety import HOST, HOST_PORT

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def isolated(monkeypatch):
    raw = os.environ.get("INVOICEIQ_PHASE7_DB_URL", "")
    url = make_url(raw) if raw else None

    assert url is not None, "Test database URL required"
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == HOST
    assert url.port == HOST_PORT
    assert url.database
    assert url.database == "invoiceiq_test"
    assert url.database != "recovery_db"

    monkeypatch.setenv("INVOICEIQ_PHASE7_ROUTING", "1")
    monkeypatch.setenv("INVOICEIQ_PHASE7_REMOTE", "0")

    # Use the application's real deterministic reasoning functions.
    # External model providers must not be called by these tests.
    async def local_diagnose(invoice, customer):
        return (
            llm_client._rule_based_diagnosis(invoice, customer),
            "rule-based-fallback",
        )

    async def local_recommend(invoice, customer, diagnosis, count):
        return (
            llm_client._rule_based_intervention(invoice, count),
            "rule-based-fallback",
        )

    monkeypatch.setattr(graph, "diagnose", local_diagnose)
    monkeypatch.setattr(graph, "recommend", local_recommend)

    class Recorder:
        def __init__(self):
            self.events = []

        async def publish(self, topic, key, payload):
            self.events.append(topic)

    recorder = Recorder()

    # The real application writes events through the
    # transactional outbox, not graph.get_publisher.
    # Observe events but preserve their database persistence.
    from app.events.outbox import TransactionalEventPublisher

    original_publish = TransactionalEventPublisher.publish

    async def record_outbox_publish(self, *args, **kwargs):
        topic = args[0] if args else kwargs.get("topic")
        recorder.events.append(topic)
        return await original_publish(self, *args, **kwargs)

    monkeypatch.setattr(
        TransactionalEventPublisher,
        "publish",
        record_outbox_publish,
    )

    from app.tools.mock_tools import execute_mock_action

    monkeypatch.setattr(
        graph, "execute_mock_action", execute_mock_action
    )
    monkeypatch.setattr(
        review_gate, "execute_mock_action", execute_mock_action
    )

    engine = create_async_engine(
        raw,
        poolclass=NullPool,
    )

    numbers = []

    try:
        async with engine.connect() as connection:
            outer = await connection.begin()

            try:
                await connection.execute(
                    text("SET LOCAL statement_timeout = '15000ms'")
                )

                async with AsyncSession(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                ) as db:
                    yield db, numbers

            finally:
                await outer.rollback()

        # Independent connection verifies rollback after test completion.
        async with engine.connect() as verify:
            if numbers:
                remaining = await verify.scalar(
                    select(Invoice.id)
                    .where(Invoice.invoice_number.in_(numbers))
                    .limit(1)
                )

                assert remaining is None, (
                    "Test fixture survived outer transaction rollback"
                )

    finally:
        await engine.dispose()


async def seed(db, numbers, *, amount, overdue, terminal=False):
    company = Company(
        name=f"P7B-{uuid4().hex[:12]}",
        industry="Integration test",
        segment=CompanySegment.MID_MARKET,
    )

    db.add(company)
    await db.flush()

    due = utc_today() - timedelta(days=overdue)

    invoice = Invoice(
        company_id=company.id,
        invoice_number=f"P7B-{uuid4().hex}",
        amount_total=Decimal(str(amount)),
        amount_paid=Decimal("0"),
        issue_date=due - timedelta(days=30),
        posting_date=due - timedelta(days=30),
        due_date=due,
        currency="INR",
        status=InvoiceStatus.OVERDUE,
    )

    db.add(invoice)
    await db.flush()

    case = RecoveryCase(
        company_id=company.id,
        invoice_id=invoice.id,
        status=(
            RecoveryCaseStatus.CLOSED
            if terminal else RecoveryCaseStatus.OPEN
        ),
        revenue_at_risk=invoice.amount_total,
        recovered_amount=Decimal("0"),
    )

    db.add(case)
    await db.flush()

    numbers.append(invoice.invoice_number)
    return case


async def rows(db, model, case_id):
    result = await db.scalars(
        select(model).where(model.recovery_case_id == case_id)
    )
    return list(result.all())


async def test_approved_and_cooldown(isolated):
    db, numbers = isolated

    case = await seed(
        db, numbers,
        amount="150000.00",
        overdue=5,
    )

    first = await run_recovery_cycle(db, case.id)

    assert first["policy_decision"] == "APPROVED"
    assert first["final_action"] == "SEND_EMAIL"

    actions = await rows(db, RecoveryAction, case.id)
    assert len(actions) == 1
    assert actions[0].status == RecoveryActionStatus.EXECUTED

    assert len(await rows(db, AgentDecision, case.id)) == 2
    assert len(await rows(db, CommunicationLog, case.id)) == 1
    assert len(await rows(db, AuditLog, case.id)) >= 4

    decision = (
        await db.scalars(
            select(PolicyDecision).where(
                PolicyDecision.recovery_action_id == actions[0].id
            )
        )
    ).one()

    assert decision.decision == PolicyDecisionResult.APPROVED

    # Repeating immediately must not generate another contact.
    repeat = await run_recovery_cycle(db, case.id)

    assert repeat["policy_decision"] == "REJECTED"
    assert len(await rows(db, CommunicationLog, case.id)) == 1


async def test_human_hold_and_approval(isolated):
    db, numbers = isolated

    case = await seed(
        db, numbers,
        amount="1100000.00",
        overdue=10,
    )

    state = await run_recovery_cycle(db, case.id)

    assert state["policy_decision"] == "REQUIRES_HUMAN_REVIEW"
    assert "action_result" not in state

    actions = await rows(db, RecoveryAction, case.id)

    assert len(actions) == 1
    assert (
        actions[0].status
        == RecoveryActionStatus.PENDING_APPROVAL
    )

    assert not await rows(db, CommunicationLog, case.id)

    held = await run_recovery_cycle(db, case.id)

    assert held.get("awaiting_review") is True
    assert len(await rows(db, RecoveryAction, case.id)) == 1

    approved = await review_gate.approve(
        db, actions[0].id,
        "phase7b-tester",
        "Test-only approval",
    )

    assert approved["decision"] == "APPROVED"
    assert len(await rows(db, CommunicationLog, case.id)) == 1

    with pytest.raises(review_gate.ReviewError):
        await review_gate.approve(
            db, actions[0].id,
            "phase7b-tester",
            "Duplicate approval",
        )

    assert len(await rows(db, CommunicationLog, case.id)) == 1


async def test_rejected_and_terminal_never_contact(isolated):
    db, numbers = isolated

    case = await seed(
        db, numbers,
        amount="1100000.00",
        overdue=10,
    )

    state = await run_recovery_cycle(db, case.id)

    assert state["policy_decision"] == "REQUIRES_HUMAN_REVIEW"

    actions = await rows(db, RecoveryAction, case.id)

    await review_gate.reject(
        db, actions[0].id,
        "phase7b-tester",
        "Test-only rejection",
    )

    assert not await rows(db, CommunicationLog, case.id)

    terminal = await seed(
        db, numbers,
        amount="75000.00",
        overdue=12,
        terminal=True,
    )

    final = await run_recovery_cycle(db, terminal.id)

    assert final["terminal"] is True
    assert not await rows(db, RecoveryAction, terminal.id)
    assert not await rows(db, CommunicationLog, terminal.id)
