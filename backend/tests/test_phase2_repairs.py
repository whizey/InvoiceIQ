"""Rollback-only PostgreSQL regressions for Phase 2 financial presentation."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.analytics_auth import AnalyticsIdentity
from app.core.db import async_session_factory, engine
from app.models import Company, Invoice, DatasetImport
from app.models.enums import CompanySegment, InvoiceStatus
from app.schemas.analytics import AnalyticsRange
from app.services.date_analytics import get_date_analytics
from app.api.invoices import invoice_evidence


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF])
async def test_excluded_invoice_status_and_balance_agree_across_summary_table_and_detail(status, monkeypatch):
    await engine.dispose()
    try:
        async with async_session_factory() as db:
            company=Company(name='Synthetic Phase 2 excluded invoice',segment=CompanySegment.UNKNOWN)
            db.add(company)
            await db.flush()
            invoice=Invoice(company_id=company.id,invoice_number='B2B-'+uuid4().hex,
                amount_total=Decimal('100'),amount_paid=0,currency='INR',
                issue_date=date(2015,1,1),posting_date=date(2015,1,1),due_date=date(2015,1,8),status=status)
            db.add(invoice)
            await db.flush()
            # Test-only source metadata; never replace or commit a real import.
            original_get=db.get
            async def get(entity, *args, **kwargs):
                if entity is DatasetImport:
                    return SimpleNamespace(dataset_as_of=date(2015,1,31))
                return await original_get(entity,*args,**kwargs)
            monkeypatch.setattr(db,'get',get)
            identity=AnalyticsIdentity('excluded-test',(company.id,))
            interval=AnalyticsRange(start_date=date(2015,1,1),end_date=date(2015,1,31),view='invoices')
            data=await get_date_analytics(db,interval,identity)
            row=data.historical_invoice_balances[str(invoice.id)]
            assert data.financial.outstanding_amount == data.financial.overdue_amount == 0
            assert row['status'] == status.value
            assert row['outstanding'] == 0
            filtered=await get_date_analytics(db,interval.model_copy(update={'invoice_status':status}),identity)
            assert filtered.invoice_total == 1
            assert filtered.invoices[0].id == invoice.id
            evidence=await invoice_evidence(invoice.id,as_of=date(2015,1,31),db=db)
            assert evidence.historical_balance == 0
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_model_integrity_is_verified_once_per_response_and_rechecked_on_next_request(monkeypatch):
    from app.ml import champion_serving
    from app.ml.payment_risk_model import PaymentRiskArtifactError
    original=champion_serving.verified_contract
    calls=[]
    def verify():
        calls.append(True)
        if len(calls)>1:
            raise PaymentRiskArtifactError('Changed artifact in a subsequent request')
        return original()
    monkeypatch.setattr(champion_serving,'verified_contract',verify)
    await engine.dispose()
    try:
        async with async_session_factory() as db:
            company=Company(name='Synthetic Phase 2 per-request integrity',segment=CompanySegment.UNKNOWN)
            db.add(company)
            await db.flush()
            interval=AnalyticsRange(start_date=date(2015,1,1),end_date=date(2015,1,31))
            identity=AnalyticsIdentity('integrity-test',(company.id,))
            data=await get_date_analytics(db,interval,identity)
            assert data.stored_predictions==0
            assert len(calls)==1
            with pytest.raises(PaymentRiskArtifactError,match='Changed artifact'):
                await get_date_analytics(db,interval,identity)
            assert len(calls)==2
            await db.rollback()
    finally:
        await engine.dispose()
