"""Read-only SQL analytics for the isolated, anonymized public corpus."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator
from sqlalchemy import Date, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models import Invoice, Payment, DatasetImport, Company
from app.models.enums import PaymentStatus

router = APIRouter(prefix='/analytics', tags=['public dataset analytics'])


class Window(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    grain: Literal['month', 'quarter', 'year'] = 'month'

    @model_validator(mode='after')
    def validate_order(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError('start_date must not exceed end_date')
        return self


async def public_dataset_db(session: AsyncSession = Depends(get_db)):
    if os.getenv('DEMO_SYNTHETIC', '').lower() == 'true':
        raise HTTPException(404, 'Public corpus API is unavailable in the synthetic demo')
    if os.getenv('PUBLIC_READ_ONLY', '').lower() != 'true':
        raise HTTPException(404, 'Public dataset API is disabled')
    # Fail closed if a deploy accidentally points at the development database.
    from app.seed.load_real_dataset import require_isolated_url
    try:
        require_isolated_url(str(session.bind.url))
    except ValueError:
        raise HTTPException(503, 'Public dataset database is not isolated') from None
    import re
    names = (await session.scalars(select(Company.name))).all()
    if any(not re.fullmatch(r'Customer \d{4}', name) for name in names):
        raise HTTPException(503, 'Public dataset requires pseudonymized customer names')
    return session


def population():
    return Invoice.invoice_number.like('B2B-%')


async def bounds(session, window):
    imported = await session.get(DatasetImport, 'public_b2b_dataset')
    earliest = await session.scalar(select(func.min(Invoice.posting_date)).where(population()))
    if earliest is None or imported is None:
        raise HTTPException(503, 'Public dataset has not been imported')
    as_of = imported.dataset_as_of
    start, end = window.start_date or earliest, window.end_date or as_of
    if start > end:
        raise HTTPException(422, 'start_date must not exceed end_date')
    return start, end, as_of


def result(rows, start, end, as_of, grain):
    return {'query_window': {'start_date': start, 'end_date': end, 'grain': grain, 'timezone': 'UTC', 'end_inclusive': True}, 'dataset_as_of': as_of, 'row_count': sum(row.get('count', 0) for row in rows), 'generated_at': datetime.now(timezone.utc), 'series': rows}


@router.get('/metadata')
async def metadata(session: AsyncSession = Depends(public_dataset_db)):
    start, end, as_of = await bounds(session, Window())
    count, amount = (await session.execute(select(func.count(Invoice.id), func.sum(Invoice.amount_total)).where(population()))).one()
    return {'dataset_as_of': as_of, 'first_posting_date': start, 'row_count': count, 'amount_total': amount, 'currency': 'INR', 'source': 'public B2B invoice dataset'}


@router.get('/invoiced')
async def invoiced(window: Annotated[Window, Query()], session: AsyncSession = Depends(public_dataset_db)):
    start, end, as_of = await bounds(session, window)
    bucket = func.date_trunc(window.grain, Invoice.posting_date)
    records = (await session.execute(select(bucket.label('period'), func.sum(Invoice.amount_total).label('amount'), func.count(Invoice.id).label('count')).where(population(), Invoice.posting_date.between(start, end)).group_by(bucket).order_by(bucket))).mappings().all()
    return result([dict(row) for row in records], start, end, as_of, window.grain)


@router.get('/collected')
async def collected(window: Annotated[Window, Query()], session: AsyncSession = Depends(public_dataset_db)):
    start, end, as_of = await bounds(session, window)
    bucket = func.date_trunc(window.grain, Payment.payment_date, 'UTC')
    begin = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    stop = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    records = (await session.execute(select(bucket.label('period'), func.sum(Payment.amount).label('amount'), func.count(Payment.id).label('count')).join(Invoice, Invoice.id == Payment.invoice_id).where(population(), Payment.status == PaymentStatus.SUCCESS, Payment.payment_date >= begin, Payment.payment_date < stop).group_by(bucket).order_by(bucket))).mappings().all()
    return result([dict(row) for row in records], start, end, as_of, window.grain)


@router.get('/late-rate')
async def late_rate(window: Annotated[Window, Query()], session: AsyncSession = Depends(public_dataset_db)):
    start, end, as_of = await bounds(session, window)
    # One successful clearing payment per imported invoice. Actual rate is strict
    # clearing-date lateness, explicitly distinct from the weekend-adjusted ML target.
    bucket = func.date_trunc(window.grain, Invoice.posting_date)
    cleared = func.timezone('UTC', Payment.payment_date).cast(Date)
    records = (await session.execute(select(bucket.label('period'), func.count(Invoice.id).label('count'), func.avg(case((cleared > Invoice.due_date, Decimal(1)), else_=Decimal(0))).label('actual_late_rate'), func.avg(Invoice.late_payment_probability).label('mean_predicted_probability')).join(Payment, Payment.invoice_id == Invoice.id).where(population(), Payment.status == PaymentStatus.SUCCESS, Invoice.posting_date.between(start, end)).group_by(bucket).order_by(bucket))).mappings().all()
    return {**result([dict(row) for row in records], start, end, as_of, window.grain), 'actual_definition': 'clearing_date > due_date; not weekend-adjusted', 'prediction_target': 'late_payment_weekend_adjusted'}


@router.get('/overdue-aging')
async def aging(window: Annotated[Window, Query()], session: AsyncSession = Depends(public_dataset_db)):
    start, end, as_of = await bounds(session, window)
    stop = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    paid = select(Payment.invoice_id.label('invoice_id'), func.sum(Payment.amount).label('paid')).where(Payment.status == PaymentStatus.SUCCESS, Payment.payment_date < stop).group_by(Payment.invoice_id).subquery()
    balance = Invoice.amount_total - func.coalesce(paid.c.paid, 0)
    age = literal(end, type_=Date) - Invoice.due_date
    bucket = case((age <= 30, '0–30'), (age <= 60, '31–60'), (age <= 90, '61–90'), else_='90+')
    records = (await session.execute(select(bucket.label('bucket'), func.count(Invoice.id).label('count'), func.sum(balance).label('amount')).outerjoin(paid, paid.c.invoice_id == Invoice.id).where(population(), Invoice.posting_date.between(start, end), Invoice.due_date < end, balance > 0).group_by(bucket))).mappings().all()
    return {**result([dict(row) for row in records], start, end, as_of, window.grain), 'definition': 'Posting-date cohort; unpaid balance reconstructed from successful payments through inclusive end_date'}


@router.get('/risk-distribution')
async def risk(window: Annotated[Window, Query()], session: AsyncSession = Depends(public_dataset_db)):
    start, end, as_of = await bounds(session, window)
    bucket = case((Invoice.late_payment_probability >= Decimal('.7'), 'high'), (Invoice.late_payment_probability >= Decimal('.3'), 'medium'), (Invoice.late_payment_probability.is_not(None), 'low'), else_='unscored')
    records = (await session.execute(select(bucket.label('risk'), func.count(Invoice.id).label('count')).where(population(), Invoice.posting_date.between(start, end)).group_by(bucket))).mappings().all()
    return result([dict(row) for row in records], start, end, as_of, window.grain)


@router.get('/policy-actions')
async def policy_actions(window: Annotated[Window, Query()], session: AsyncSession = Depends(public_dataset_db)):
    from app.models import RecoveryCase, RecoveryAction
    start, end, as_of = await bounds(session, window)
    records = (await session.execute(select(RecoveryAction.action_type.label('action'), func.count(RecoveryAction.id).label('count')).join(RecoveryCase, RecoveryCase.id == RecoveryAction.recovery_case_id).join(Invoice, Invoice.id == RecoveryCase.invoice_id).where(population(), Invoice.posting_date.between(start, end)).group_by(RecoveryAction.action_type))).mappings().all()
    return result([dict(row) for row in records], start, end, as_of, window.grain)


@router.get('/overrides')
async def overrides(window: Annotated[Window, Query()], session: AsyncSession = Depends(public_dataset_db)):
    from app.models import RecoveryCase, RecoveryAction
    start, end, as_of = await bounds(session, window)
    count = await session.scalar(select(func.count(RecoveryAction.id)).join(RecoveryCase, RecoveryCase.id == RecoveryAction.recovery_case_id).join(Invoice, Invoice.id == RecoveryCase.invoice_id).where(population(), Invoice.posting_date.between(start, end), RecoveryAction.recommended_action_type.is_not(None), RecoveryAction.recommended_action_type != RecoveryAction.action_type))
    return result([{'count': count}], start, end, as_of, window.grain)


@router.get('/invoices')
async def invoice_records(window: Annotated[Window, Query()], page: int = 1, search: str = '', sort: Literal['date', 'amount', 'risk'] = 'date', session: AsyncSession = Depends(public_dataset_db)):
    from app.models import Company, RecoveryCase
    if not 1 <= page <= 10000 or len(search) > 100:
        raise HTTPException(422, 'Invalid page or search')
    start, end, as_of = await bounds(session, window)
    filters = [population(), Invoice.posting_date.between(start, end)]
    if search:
        pattern = '%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        filters.append(Invoice.invoice_number.ilike(pattern, escape='\\') | Company.name.ilike(pattern, escape='\\'))
    count = await session.scalar(select(func.count(Invoice.id)).join(Company).where(*filters))
    column = {'date': Invoice.posting_date, 'amount': Invoice.amount_total, 'risk': Invoice.late_payment_probability}[sort]
    records = (await session.execute(select(Invoice.id, Invoice.invoice_number, Company.name.label('customer'), Invoice.amount_total.label('amount'), Invoice.posting_date, Invoice.due_date, Invoice.late_payment_probability.label('probability'), Invoice.payment_risk_drivers.label('drivers'), Invoice.status, RecoveryCase.id.label('case_id')).join(Company).outerjoin(RecoveryCase, RecoveryCase.invoice_id == Invoice.id).where(*filters).order_by(column.desc().nulls_last(), Invoice.id).offset((page - 1) * 25).limit(25))).mappings().all()
    return {'query_window': {'start_date': start, 'end_date': end}, 'dataset_as_of': as_of, 'row_count': count, 'generated_at': datetime.now(timezone.utc), 'page': page, 'page_size': 25, 'records': [dict(row) for row in records]}


# One aggregate implementation and the same scoped authentication contract.
from app.api.dashboard import dashboard_analytics  # noqa: E402
from app.schemas.analytics import AnalyticsOut  # noqa: E402
router.add_api_route("", dashboard_analytics, methods=["GET"], response_model=AnalyticsOut)
