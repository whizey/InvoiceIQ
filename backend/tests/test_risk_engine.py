import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.db import async_session_factory, engine
from app.models import Company, Invoice
from app.models.enums import CompanySegment, InvoiceStatus
from app.seed.run import seed
from app.services.risk_engine import run_detection
from app.core.time import utc_today

# All tests in this module share one event loop (rather than pytest-asyncio's
# default of a fresh loop per test function) — the async SQLAlchemy engine's
# pooled asyncpg connections are bound to whatever loop opened them, and reusing
# a pooled connection on a different (and by then closed) loop raises
# "RuntimeError: Event loop is closed". See tests/test_api_read_endpoints.py
# for the equivalent fix on the sync TestClient side.
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    async def prepare():
        try:
            await seed()
        finally:
            # The module test loop must open its own pooled connections.
            await engine.dispose()

    asyncio.run(prepare())
    yield
    asyncio.run(engine.dispose())


async def test_detection_is_idempotent_against_seeded_data():
    # The seeded overdue invoices already have hand-built cases (Phase 3);
    # a detection pass over them should find nothing new to do.
    async with async_session_factory() as session:
        result = await run_detection(session)

    assert result.invoices_marked_overdue == []
    assert result.cases_created == []


async def test_detection_creates_case_for_newly_overdue_invoice():
    due = utc_today() - timedelta(days=3)

    async with async_session_factory() as session:
        company = Company(name="Test Freshco", industry="Testing", segment=CompanySegment.SMB)
        session.add(company)
        await session.flush()

        invoice = Invoice(
            company_id=company.id,
            invoice_number="INV-TESTFRESH-0001",
            amount_total=Decimal("50000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.SENT,
        )
        session.add(invoice)
        await session.commit()
        invoice_id = invoice.id

    async with async_session_factory() as session:
        result = await run_detection(session)

    marked_ids = {inv.id for inv in result.invoices_marked_overdue}
    assert invoice_id in marked_ids

    matching_cases = [c for c in result.cases_created if c.invoice_id == invoice_id]
    assert len(matching_cases) == 1
    case = matching_cases[0]
    assert case.revenue_at_risk == Decimal("50000.00")
    assert case.recovery_window_deadline == due + timedelta(days=90)

    # Recovery cases reuse the invoice's B2B late-payment prediction.
    # No synthetic recovery probability is generated.
    async with async_session_factory() as session:
        scored_invoice = await session.get(Invoice, invoice_id)
        probability = scored_invoice.late_payment_probability

    assert case.recovery_probability is None

    if probability is None:
        # Model disabled, unavailable, or missing required inputs.
        assert case.risk_score is None
        assert case.risk_level is None
    else:
        from app.models.enums import RiskLevel

        expected_score = Decimal(
            str(round(float(probability) * 100, 2))
        )
        expected_level = (
            RiskLevel.HIGH if probability >= Decimal("0.70")
            else RiskLevel.MEDIUM if probability >= Decimal("0.35")
            else RiskLevel.LOW
        )

        assert case.risk_score == expected_score
        assert case.risk_level == expected_level
        assert scored_invoice.payment_risk_model_version is not None

    async with async_session_factory() as session:
        result2 = await run_detection(session)
    assert not any(c.invoice_id == invoice_id for c in result2.cases_created)
    assert not any(inv.id == invoice_id for inv in result2.invoices_marked_overdue)


async def test_partially_paid_overdue_invoice_uses_outstanding_balance():
    due = utc_today() - timedelta(days=1)

    async with async_session_factory() as session:
        company = Company(name="Test Partial Co", industry="Testing", segment=CompanySegment.SMB)
        session.add(company)
        await session.flush()

        invoice = Invoice(
            company_id=company.id,
            invoice_number="INV-TESTPARTIAL-0001",
            amount_total=Decimal("100000.00"),
            amount_paid=Decimal("40000.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.PARTIALLY_PAID,
        )
        session.add(invoice)
        await session.commit()
        invoice_id = invoice.id

    async with async_session_factory() as session:
        result = await run_detection(session)

    case = next(c for c in result.cases_created if c.invoice_id == invoice_id)
    assert case.revenue_at_risk == Decimal("60000.00")
