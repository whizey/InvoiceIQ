import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.db import async_session_factory, engine
from app.models import Company, Invoice, RecoveryCase
from app.models.enums import CompanySegment, InvoiceStatus, RecoveryCaseStatus
from app.seed.run import seed
from app.services.risk_context import build_risk_features
from app.core.time import utc_today

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    asyncio.run(seed())
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def _get_invoice(invoice_number: str) -> Invoice:
    async with async_session_factory() as session:
        result = await session.execute(select(Invoice).where(Invoice.invoice_number == invoice_number))
        invoice = result.scalar_one()
        return await build_risk_features(session, invoice)


async def test_risk_features_reflect_bluepeaks_repeated_late_payments():
    # Bluepeak's seeded history: paid 15, -2, 18, 15 days after due —
    # 3 late, 1 on-time, avg delay 11.5.
    features = await _get_invoice("INV-BLUEPEAK-2005")

    assert features.num_prior_late_payments == 3
    assert features.num_prior_on_time_payments == 1
    assert features.avg_historical_payment_delay == pytest.approx(11.5)
    assert features.days_overdue == 30
    assert features.outstanding_balance == pytest.approx(210_000.0)


async def test_risk_features_use_neutral_prior_for_customer_with_no_case_history():
    # None of the seeded companies have a prior CLOSED/CLOSED_UNRECOVERED
    # case yet (Aarav's case is CLOSED but it's the invoice under test
    # elsewhere) — Vertex specifically has zero prior recovery cases.
    features = await _get_invoice("INV-VERTEX-3010")

    assert features.prior_recovery_success_rate == 0.5
    assert features.days_overdue == 60
    assert features.outstanding_balance == pytest.approx(1_800_000.0)


async def test_risk_features_computes_real_prior_recovery_success_rate():
    # A company with an actual track record (2 recovered, 1 not) should get
    # a computed rate, not the 0.5 neutral default used for a clean slate.
    async with async_session_factory() as session:
        company = Company(name="Track Record Co", industry="Testing", segment=CompanySegment.MID_MARKET)
        session.add(company)
        await session.flush()

        for i, (status, recovered) in enumerate(
            [
                (RecoveryCaseStatus.CLOSED, Decimal("50000.00")),
                (RecoveryCaseStatus.CLOSED, Decimal("30000.00")),
                (RecoveryCaseStatus.CLOSED_UNRECOVERED, Decimal("0.00")),
            ]
        ):
            due = utc_today() - timedelta(days=200 - i * 30)
            past_invoice = Invoice(
                company_id=company.id,
                invoice_number=f"INV-TRACKREC-{i}",
                amount_total=Decimal("50000.00"),
                amount_paid=recovered,
                issue_date=due - timedelta(days=30),
                due_date=due,
                status=InvoiceStatus.PAID if recovered > 0 else InvoiceStatus.WRITTEN_OFF,
            )
            session.add(past_invoice)
            await session.flush()

            session.add(
                RecoveryCase(
                    invoice_id=past_invoice.id,
                    company_id=company.id,
                    status=status,
                    revenue_at_risk=Decimal("50000.00"),
                    recovered_amount=recovered,
                )
            )

        due = utc_today() - timedelta(days=10)
        current_invoice = Invoice(
            company_id=company.id,
            invoice_number="INV-TRACKREC-CURRENT",
            amount_total=Decimal("40000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.OVERDUE,
        )
        session.add(current_invoice)
        await session.commit()

        features = await build_risk_features(session, current_invoice)

    assert features.prior_recovery_success_rate == pytest.approx(2 / 3)
