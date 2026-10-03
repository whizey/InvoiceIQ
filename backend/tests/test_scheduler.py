import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.db import async_session_factory, engine
from app.core.locks import acquire_lock
from app.models import Company, Invoice, RecoveryAction, RecoveryCase
from app.models.enums import CompanySegment, InvoiceStatus, RecoveryCaseStatus
from app.services import scheduler as scheduler_mod
from app.services.scheduler import run_scheduler_tick, start_scheduler
from app.core.time import utc_today


@pytest.fixture(scope="module", autouse=True)
def _dispose_engine():
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


@pytest.mark.asyncio(loop_scope="module")
async def test_scheduler_tick_detects_overdue_and_runs_a_cycle():
    async with async_session_factory() as session:
        company = Company(
            name="Scheduler Test Co", industry="Testing", segment=CompanySegment.SMB
        )
        session.add(company)
        await session.flush()
        due = utc_today() - timedelta(days=5)
        invoice = Invoice(
            company_id=company.id,
            invoice_number="INV-SCHED-001",
            amount_total=Decimal("50000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.SENT,
        )
        session.add(invoice)
        await session.commit()
        invoice_id = invoice.id

    summary = await run_scheduler_tick()
    assert summary["invoices_marked_overdue"] >= 1
    assert summary["cases_created"] >= 1
    assert summary["cycles_run"] >= 1

    async with async_session_factory() as session:
        case = (
            await session.execute(
                select(RecoveryCase).where(RecoveryCase.invoice_id == invoice_id)
            )
        ).scalar_one()
        actions = (
            await session.execute(
                select(func.count()).where(RecoveryAction.recovery_case_id == case.id)
            )
        ).scalar_one()
    assert actions >= 1
    assert case.status == RecoveryCaseStatus.OPEN


@pytest.mark.asyncio(loop_scope="module")
async def test_scheduler_skips_escalated_cases():
    async with async_session_factory() as session:
        company = Company(
            name="Scheduler Escalated Co", industry="Testing", segment=CompanySegment.SMB
        )
        session.add(company)
        await session.flush()
        due = utc_today() - timedelta(days=10)
        invoice = Invoice(
            company_id=company.id,
            invoice_number="INV-SCHED-ESC-001",
            amount_total=Decimal("10000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.OVERDUE,
        )
        session.add(invoice)
        await session.flush()
        case = RecoveryCase(
            invoice_id=invoice.id,
            company_id=company.id,
            status=RecoveryCaseStatus.ESCALATED,
            revenue_at_risk=Decimal("10000.00"),
        )
        session.add(case)
        await session.commit()
        case_id = case.id
        before_count = (
            await session.execute(
                select(func.count()).where(RecoveryAction.recovery_case_id == case_id)
            )
        ).scalar_one()

    await run_scheduler_tick()

    async with async_session_factory() as session:
        after_count = (
            await session.execute(
                select(func.count()).where(RecoveryAction.recovery_case_id == case_id)
            )
        ).scalar_one()
        status = (
            await session.execute(select(RecoveryCase.status).where(RecoveryCase.id == case_id))
        ).scalar_one()
    assert after_count == before_count
    assert status == RecoveryCaseStatus.ESCALATED


@pytest.mark.asyncio(loop_scope="module")
async def test_scheduler_skips_case_when_lock_held():
    async with async_session_factory() as session:
        company = Company(
            name="Scheduler Locked Co", industry="Testing", segment=CompanySegment.SMB
        )
        session.add(company)
        await session.flush()
        due = utc_today() - timedelta(days=8)
        invoice = Invoice(
            company_id=company.id,
            invoice_number="INV-SCHED-LOCK-001",
            amount_total=Decimal("20000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.OVERDUE,
        )
        session.add(invoice)
        await session.flush()
        case = RecoveryCase(
            invoice_id=invoice.id,
            company_id=company.id,
            status=RecoveryCaseStatus.OPEN,
            revenue_at_risk=Decimal("20000.00"),
        )
        session.add(case)
        await session.commit()
        case_id = case.id

    async with acquire_lock(f"recovery-case:{case_id}"):
        summary = await run_scheduler_tick()

    assert summary["cycles_skipped_locked"] >= 1

    async with async_session_factory() as session:
        action_count = (
            await session.execute(
                select(func.count()).where(RecoveryAction.recovery_case_id == case_id)
            )
        ).scalar_one()
    assert action_count == 0


def test_start_scheduler_is_noop_when_disabled():
    start_scheduler()
    assert scheduler_mod._task is None
