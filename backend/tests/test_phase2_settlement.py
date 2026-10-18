"""Regression coverage for first full settlement and historical observation cutoffs."""
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.dialects import postgresql

from app.core.analytics_auth import AnalyticsIdentity
from app.schemas.analytics import AnalyticsRange
from app.services.date_analytics import _financial_metrics


@pytest.mark.asyncio
@pytest.mark.parametrize('name,due,status,payments,cutoff,balance,resolved,delay', [
    ('fully-paid', '2015-01-08', 'PAID', [('2015-01-07T00:00:00+00:00', '100')], '2015-01-15', '0', 1, 0),
    ('unpaid', '2015-01-08', 'SENT', [], '2015-01-15', '100', 0, None),
    ('partial', '2015-01-08', 'PARTIALLY_PAID', [('2015-01-07T00:00:00+00:00', '40')], '2015-01-15', '60', 0, None),
    ('multiple-partials', '2015-01-08', 'PAID', [('2015-01-05T00:00:00+00:00', '20'), ('2015-01-07T00:00:00+00:00', '30'), ('2015-01-10T00:00:00+00:00', '50')], '2015-01-15', '0', 1, 2),
    ('same-timestamp', '2015-01-08', 'PAID', [('2015-01-10T12:00:00+00:00', '40'), ('2015-01-10T12:00:00+00:00', '60')], '2015-01-15', '0', 1, 2),
    ('future-partial', '2015-01-08', 'PARTIALLY_PAID', [('2015-01-07T00:00:00+00:00', '40'), ('2015-01-20T00:00:00+00:00', '20')], '2015-01-15', '60', 0, None),
    ('exact-due', '2015-01-08', 'PAID', [('2015-01-08T23:59:59+00:00', '100')], '2015-01-15', '0', 1, 0),
    ('saturday', '2015-01-10', 'PAID', [('2015-01-12T00:00:00+00:00', '100')], '2015-01-15', '0', 1, 0),
    ('sunday', '2015-01-11', 'PAID', [('2015-01-13T00:00:00+00:00', '100')], '2015-01-15', '0', 1, 1),
    ('chronological-topup', '2015-01-08', 'PAID', [('2015-01-14T00:00:00+00:00', '10'), ('2015-01-10T00:00:00+00:00', '60'), ('2015-01-05T00:00:00+00:00', '40')], '2015-01-15', '0', 1, 2),
    ('utc-end-inclusive', '2015-01-08', 'PAID', [('2015-01-11T05:29:59+05:30', '100')], '2015-01-10', '0', 1, 2),
    ('utc-next-day-excluded', '2015-01-08', 'PAID', [('2015-01-11T05:30:00+05:30', '100')], '2015-01-10', '100', 0, None),
    ('cancelled', '2015-01-08', 'CANCELLED', [], '2015-01-15', '0', 0, None),
    ('written-off', '2015-01-08', 'WRITTEN_OFF', [], '2015-01-15', '0', 0, None),
    ('cancelled-settled', '2015-01-08', 'CANCELLED', [('2015-01-10T00:00:00+00:00', '100')], '2015-01-15', '0', 1, 2),
    ('written-off-settled', '2015-01-08', 'WRITTEN_OFF', [('2015-01-10T00:00:00+00:00', '100')], '2015-01-15', '0', 1, 2),
])
async def test_postgres_settlement_edge_cases(name, due, status, payments, cutoff, balance, resolved, delay):
    """Owned rollback-only fixtures exercise real SQL under a non-UTC session."""
    from sqlalchemy import text
    from app.core.db import async_session_factory, engine
    from app.models import Company, Invoice, Payment
    from app.models.enums import CompanySegment, InvoiceStatus, PaymentMethod, PaymentStatus
    await engine.dispose()
    try:
        async with async_session_factory() as db:
            await db.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))
            company = Company(name='Synthetic settlement ' + name, segment=CompanySegment.UNKNOWN)
            db.add(company)
            await db.flush()
            invoice = Invoice(company_id=company.id, invoice_number='B2B-' + uuid4().hex,
                amount_total=Decimal('100'), amount_paid=sum((Decimal(p[1]) for p in payments), Decimal(0)),
                currency='INR', issue_date=date(2015, 1, 1), posting_date=date(2015, 1, 1),
                due_date=date.fromisoformat(due), status=InvoiceStatus[status])
            db.add(invoice)
            await db.flush()
            for timestamp, amount in payments:
                db.add(Payment(invoice_id=invoice.id, amount=Decimal(amount), currency='INR',
                    payment_date=datetime.fromisoformat(timestamp), method=PaymentMethod.BANK_TRANSFER,
                    status=PaymentStatus.SUCCESS))
            for payment_status, currency in [(PaymentStatus.FAILED, 'INR'),
                    (PaymentStatus.PENDING, 'INR'), (PaymentStatus.SUCCESS, 'USD')]:
                db.add(Payment(invoice_id=invoice.id, amount=Decimal('999'), currency=currency,
                    payment_date=datetime(2015, 1, 2, tzinfo=timezone.utc),
                    method=PaymentMethod.BANK_TRANSFER, status=payment_status))
            await db.flush()
            result = await _financial_metrics(db,
                AnalyticsRange(start_date=date(2015, 1, 1), end_date=date(2024, 1, 1)),
                AnalyticsIdentity('edge-test', (company.id,)),
                SimpleNamespace(dataset_as_of=date.fromisoformat(cutoff)))
            assert result.observed_invoice_count == 1
            assert result.outstanding_amount == Decimal(balance)
            assert result.overdue_amount == Decimal(balance)
            assert result.overdue_invoice_count == int(Decimal(balance) > 0)
            assert result.resolved_invoice_count == resolved
            assert result.late_invoice_count == int(delay is not None and delay > 0)
            assert result.average_delay_days == result.median_delay_days == delay
            assert result.actual_late_rate == (int(delay > 0) if resolved else None)
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_financial_query_scopes_payments_and_uses_first_observable_settlement():
    # Compile the actual analytics statements even where PostgreSQL is unavailable.
    empty = SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))
    aggregate = SimpleNamespace(one=lambda: (0, Decimal(0), Decimal(0), 0))
    labels = SimpleNamespace(one=lambda: (0, 0, None, None))
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[None, 0, 0]),
        execute=AsyncMock(side_effect=[empty, aggregate, empty, labels, empty]))
    interval = AnalyticsRange(start_date=date(2015, 1, 1), end_date=date(2024, 1, 1))
    result = await _financial_metrics(db, interval, AnalyticsIdentity('test', (uuid4(),)),
        SimpleNamespace(dataset_as_of=date(2016, 3, 17)))
    assert result.cutoff == date(2016, 3, 17)
    query = db.execute.call_args_list[3].args[0]
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert 'min(CASE WHEN' in sql
    assert 'paid_to_date >= invoices.amount_total' in sql
    assert 'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW' in sql
    assert 'payments.invoice_id IN (SELECT invoices.id' in sql
    assert 'invoices.company_id IN' in sql
    assert datetime(2016, 3, 18, tzinfo=timezone.utc) in compiled.params.values()


@pytest.mark.asyncio
async def test_partial_settlement_topup_future_payment_and_cross_page_reconciliation():
    from app.core.db import async_session_factory, engine
    from app.models import Company, Invoice, Payment
    from app.models.enums import CompanySegment, InvoiceStatus, PaymentMethod, PaymentStatus
    from app.services.date_analytics import get_date_analytics
    await engine.dispose()
    company_id = uuid4()
    try:
        async with async_session_factory() as db:
            company = Company(id=company_id, name='Synthetic settlement regression', segment=CompanySegment.UNKNOWN)
            db.add(company)
            await db.flush()
            invoice = Invoice(company_id=company_id, invoice_number='B2B-' + uuid4().hex,
                amount_total=100, amount_paid=110, currency='INR', issue_date=date(2015, 1, 1),
                posting_date=date(2015, 1, 1), due_date=date(2015, 1, 8), status=InvoiceStatus.PAID)
            db.add(invoice)
            await db.flush()
            for day, amount in [(5, 40), (10, 60), (20, 10)]:
                db.add(Payment(invoice_id=invoice.id, amount=amount, currency='INR',
                    payment_date=datetime(2015, 1, day, tzinfo=timezone.utc),
                    method=PaymentMethod.BANK_TRANSFER, status=PaymentStatus.SUCCESS))
            await db.commit()
            identity = AnalyticsIdentity('settlement-test', (company_id,))
            imported = SimpleNamespace(dataset_as_of=date(2015, 1, 31))
            def window(end):
                return AnalyticsRange(start_date=date(2015, 1, 1), end_date=end)
            partial = await _financial_metrics(db, window(date(2015, 1, 9)), identity, imported)
            assert partial.outstanding_amount == 60
            assert partial.resolved_invoice_count == 0
            settled = await _financial_metrics(db, window(date(2015, 1, 31)), identity, imported)
            assert settled.outstanding_amount == 0
            assert settled.average_delay_days == settled.median_delay_days == 2
            assert settled.delay_series == []  # Never fabricate a trusted score.
            cutoff = await _financial_metrics(db, window(date(2024, 1, 1)), identity,
                SimpleNamespace(dataset_as_of=date(2015, 1, 9)))
            assert cutoff.outstanding_amount == 60
            assert cutoff.resolved_invoice_count == 0
            pages = [await get_date_analytics(db, window(date(2015, 1, 31)).model_copy(
                update={'view': view}), identity) for view in
                ['overview', 'invoices', 'cases', 'investigations', 'approvals', 'ml']]
            assert all(page.summary == pages[0].summary for page in pages)
            assert all(page.series == pages[0].series for page in pages)
            assert pages[0].summary.recovered_amount == 110
            assert pages[0].summary.total_invoice_amount == 100
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Company).where(Company.id == company_id))
            await db.commit()
        await engine.dispose()
