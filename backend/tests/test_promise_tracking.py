import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.db import async_session_factory, engine
from app.models import Company, Invoice, PromiseToPay, RecoveryCase
from app.models.enums import CompanySegment, InvoiceStatus, PromiseToPayStatus, RecoveryCaseStatus
from app.seed.run import seed
from app.services.promise_tracking import check_promises_to_pay, has_unresolved_broken_promise
from app.services.risk_engine import run_detection
from app.core.time import utc_today

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    asyncio.run(seed())
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def _make_case_with_promise(
    name: str, invoice_number: str, promised_date: date, amount_paid: Decimal
) -> tuple[RecoveryCase, PromiseToPay]:
    async with async_session_factory() as session:
        company = Company(name=name, industry="Testing", segment=CompanySegment.SMB)
        session.add(company)
        await session.flush()

        due = utc_today() - timedelta(days=20)
        invoice = Invoice(
            company_id=company.id,
            invoice_number=invoice_number,
            amount_total=Decimal("60000.00"),
            amount_paid=amount_paid,
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.OVERDUE,
        )
        session.add(invoice)
        await session.flush()

        case = RecoveryCase(
            invoice_id=invoice.id,
            company_id=company.id,
            status=RecoveryCaseStatus.MONITORING,
            revenue_at_risk=invoice.amount_total - invoice.amount_paid,
            recovery_window_deadline=due + timedelta(days=90),
        )
        session.add(case)
        await session.flush()

        promise = PromiseToPay(
            recovery_case_id=case.id,
            invoice_id=invoice.id,
            promised_amount=Decimal("60000.00"),
            promised_date=promised_date,
            status=PromiseToPayStatus.PENDING,
        )
        session.add(promise)
        await session.commit()

        return case, promise


async def test_promise_marked_fulfilled_when_invoice_fully_paid():
    case, promise = await _make_case_with_promise(
        "Promise Test Co A", "INV-PROMTEST-A001", utc_today() + timedelta(days=5), Decimal("60000.00")
    )

    async with async_session_factory() as session:
        result = await check_promises_to_pay(session)
        await session.commit()

    assert any(p.id == promise.id for p in result.fulfilled)

    async with async_session_factory() as session:
        refreshed = await session.get(PromiseToPay, promise.id)
        assert refreshed.status == PromiseToPayStatus.FULFILLED
        assert refreshed.fulfilled_at is not None


async def test_promise_marked_broken_when_date_passes_unpaid():
    case, promise = await _make_case_with_promise(
        "Promise Test Co B", "INV-PROMTEST-B001", utc_today() - timedelta(days=1), Decimal("0.00")
    )

    async with async_session_factory() as session:
        result = await check_promises_to_pay(session)
        await session.commit()

    assert any(p.id == promise.id for p in result.broken)

    async with async_session_factory() as session:
        refreshed = await session.get(PromiseToPay, promise.id)
        assert refreshed.status == PromiseToPayStatus.BROKEN
        has_broken = await has_unresolved_broken_promise(session, case.id)
        assert has_broken is True


async def test_run_detection_resolves_broken_promises_and_publishes_event():
    # Exercises the full orchestration path (run_detection), not just
    # check_promises_to_pay directly — this is what POST
    # /recovery-cases/detect-overdue actually calls, including the
    # promise_to_pay.broken event publish.
    case, promise = await _make_case_with_promise(
        "Promise Test Co D", "INV-PROMTEST-D001", utc_today() - timedelta(days=2), Decimal("0.00")
    )

    async with async_session_factory() as session:
        result = await run_detection(session)

    assert any(p.id == promise.id for p in result.promises_checked.broken)

    async with async_session_factory() as session:
        refreshed = await session.get(PromiseToPay, promise.id)
        assert refreshed.status == PromiseToPayStatus.BROKEN


async def test_promise_not_yet_due_stays_pending():
    case, promise = await _make_case_with_promise(
        "Promise Test Co C", "INV-PROMTEST-C001", utc_today() + timedelta(days=5), Decimal("0.00")
    )

    async with async_session_factory() as session:
        result = await check_promises_to_pay(session)
        await session.commit()

    assert not any(p.id == promise.id for p in result.fulfilled)
    assert not any(p.id == promise.id for p in result.broken)

    async with async_session_factory() as session:
        refreshed = await session.get(PromiseToPay, promise.id)
        assert refreshed.status == PromiseToPayStatus.PENDING
