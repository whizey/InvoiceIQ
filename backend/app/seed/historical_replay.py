"""Reconstruct a real historical source cohort in an explicitly isolated replay DB."""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import os

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import Company, DatasetImport, Invoice, Payment, PaymentEvent
from app.models.enums import InvoiceStatus
from app.seed.load_real_dataset import identity, read_dataset, resolve_dataset_path, require_isolated_url


def replay_selection(frame, as_of: date) -> list:
    """A real high-value review candidate plus two ordinary overdue invoices."""
    eligible = frame[(frame.Posting_Date.dt.date <= as_of)
        & (frame.Net_Due_Date.dt.date < as_of) & (frame.Clearing_date.dt.date > as_of)].copy()
    eligible['age'] = (datetime.combine(as_of, datetime.min.time()) - eligible.Net_Due_Date).dt.days
    eligible = eligible[eligible.age < 90].sort_values(['Net_Due_Date', 'Document_No'])
    review = eligible[(eligible.Amount >= 1_000_000) & (eligible.age < 45)]
    ordinary = eligible[eligible.Amount < 1_000_000]
    if review.empty or len(ordinary) < 2:
        raise ValueError('Historical cutoff lacks a source-supported review and two ordinary cases')
    return [review.iloc[0].Document_No, *ordinary.head(2).Document_No.tolist()]


async def reconstruct(as_of: date) -> dict:
    source_url = require_isolated_url(os.environ['PHASE1_SOURCE_DATABASE_URL'])
    target_url = require_isolated_url(os.environ['DATABASE_URL'])
    source, target = make_url(source_url), make_url(target_url)
    if not target.database.endswith('_replay_test') or target == source:
        raise ValueError('Historical reconstruction requires a separate *_replay_test database')
    print('HISTORICAL_REPLAY_DATABASE_IDENTITY:', {'host': target.host, 'port': target.port,
        'database': target.database, 'environment': 'phase1-historical-replay'})
    frame = read_dataset()
    selected = replay_selection(frame, as_of)
    cohort = frame[frame.Posting_Date.dt.date <= as_of]
    source_engine, target_engine = create_async_engine(source_url), create_async_engine(target_url)
    try:
        async with async_sessionmaker(source_engine)() as source_db, async_sessionmaker(target_engine)() as target_db:
            if await target_db.scalar(select(Invoice.id).limit(1)) is not None:
                raise ValueError('Replay target is already populated; refusing to overwrite a replay')
            customers = {identity('customer', int(value)) for value in cohort.Cust_Num}
            for customer in (await source_db.scalars(select(Company).where(Company.id.in_(customers)))).all():
                target_db.add(Company(id=customer.id, name=customer.name, segment=customer.segment))
            await target_db.flush()
            ids = {identity('invoice', value) for value in cohort.Document_No}
            invoices = (await source_db.scalars(select(Invoice).where(Invoice.id.in_(ids)))).all()
            rows = {identity('invoice', row.Document_No): row for row in cohort.itertuples(index=False)}
            outstanding = Decimal('0')
            cleared = 0
            for invoice in invoices:
                row = rows[invoice.id]
                paid = row.Amount if row.Clearing_date.date() <= as_of else Decimal('0')
                status = InvoiceStatus.PAID if paid == row.Amount else InvoiceStatus.OVERDUE if invoice.due_date < as_of else InvoiceStatus.SENT
                values = {column.name: getattr(invoice, column.name) for column in Invoice.__table__.columns
                          if column.name not in {'created_at', 'updated_at', 'amount_paid', 'status'}}
                target_db.add(Invoice(**values, amount_paid=paid, status=status))
                outstanding += row.Amount - paid
                cleared += int(paid == row.Amount)
            await target_db.flush()
            payments = (await source_db.scalars(select(Payment).where(Payment.invoice_id.in_(ids)))).all()
            observable_payments = [p for p in payments if p.payment_date.date() <= as_of]
            for payment in observable_payments:
                target_db.add(Payment(**{c.name: getattr(payment, c.name) for c in Payment.__table__.columns if c.name != 'created_at'}))
            events = (await source_db.scalars(select(PaymentEvent).where(PaymentEvent.invoice_id.in_(ids)))).all()
            for event in events:
                if event.occurred_at.date() <= as_of:
                    target_db.add(PaymentEvent(**{c.name: getattr(event, c.name) for c in PaymentEvent.__table__.columns if c.name != 'created_at'}))
            target_db.add(DatasetImport(source='public_b2b_historical_replay',
                source_sha256=hashlib.sha256(resolve_dataset_path().read_bytes()).hexdigest(),
                dataset_as_of=as_of, row_count=len(cohort), customer_count=len(customers),
                amount_total=sum(cohort.Amount, Decimal('0')), reconciled_at=datetime.now(timezone.utc)))
            await target_db.commit()
            return {'mode': 'simulated workflow on real invoices', 'historical_replay_as_of': as_of.isoformat(),
                'source_dataset_as_of': max(frame.Clearing_date).date().isoformat(),
                'cohort_invoices': len(invoices), 'cohort_customers': len(customers),
                'observable_payments': len(observable_payments), 'cleared_invoices': cleared,
                'outstanding_amount': str(outstanding), 'selected_source_invoices': [str(v) for v in selected],
                'selected_invoice_ids': [str(identity('invoice', v)) for v in selected]}
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
