"""Query-back acceptance for the reconciled real corpus; no financial mutations."""
import asyncio
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.db import async_session_factory, engine
from app.main import app
from app.ml.champion_serving import verified_contract
from app.services.payment_risk_context import PriorInvoiceSnapshot, compute_payment_risk_features
from app.models import Company, DatasetImport, Invoice, OfflineEvaluation, Payment, PaymentEvent
from app.models.enums import CompanySegment
from app.seed.load_real_dataset import identity, read_dataset, reconcile, require_isolated_url, resolve_dataset_path


async def main():
    require_isolated_url(os.environ['DATABASE_URL'])
    frame = read_dataset()
    contract = verified_contract()
    async with async_session_factory() as db:
        result = await reconcile(db, frame, contract)
        imported = await db.get(DatasetImport, 'public_b2b_dataset')
        assert imported.source_sha256 == hashlib.sha256(resolve_dataset_path().read_bytes()).hexdigest()
        invoices = (await db.scalars(select(Invoice))).all()
        rows = {identity('invoice', r.Document_No): r for r in frame.itertuples(index=False)}
        by_id = {invoice.id: invoice for invoice in invoices}
        payment_dates = dict((await db.execute(select(Payment.invoice_id, Payment.payment_date))).all())
        max_error = 0.0
        for invoice in invoices:
            row = rows[invoice.id]
            assert (invoice.issue_date, invoice.posting_date, invoice.due_date, payment_dates[invoice.id].date()) == (
                row.Doc_Date.date(), row.Posting_Date.date(), row.Net_Due_Date.date(), row.Clearing_date.date())
            assert invoice.amount_total == invoice.amount_paid == row.Amount
            assert invoice.payment_risk_model_version == contract['version']
            assert invoice.payment_risk_artifact_sha256 == contract['artifact_sha256']
            assert invoice.payment_risk_scored_at.tzinfo is not None
            explanation = invoice.payment_risk_explanation_metadata
            assert explanation['feature_timing'] == 'posting_time' and explanation['units'] == 'log_odds'
            assert {d['feature'] for d in invoice.payment_risk_drivers} == set(contract['metadata']['features'])
            for driver in invoice.payment_risk_drivers:
                assert math.isfinite(driver['shap_value'])
                assert driver['value'] == invoice.payment_risk_features[driver['feature']]
            margin = explanation['base_value'] + sum(d['shap_value'] for d in invoice.payment_risk_drivers)
            probability = 1 / (1 + math.exp(-margin))
            error = abs(probability - float(invoice.late_payment_probability))
            max_error = max(max_error, error)
            # PostgreSQL probability is Numeric(7,6); raw inference checks remain 1e-7.
            assert error <= 0.00000051
        groups = {customer: list(group.itertuples(index=False)) for customer, group in frame.groupby('Cust_Num')}
        for row in frame.sample(n=500, random_state=45839).itertuples(index=False):
            history = [PriorInvoiceSnapshot(str(p.Document_No), float(p.Amount), p.Posting_Date.date(),
                p.Net_Due_Date.date(), p.Clearing_date.date()) for p in groups[row.Cust_Num] if p.Posting_Date < row.Posting_Date]
            expected = compute_payment_risk_features(amount=float(row.Amount), posting_date=row.Posting_Date.date(),
                payment_term=int(row.Payment_Term), history=history).to_row()
            stored = by_id[identity('invoice', row.Document_No)].payment_risk_features
            assert set(stored) == set(expected)
            for name, value in expected.items():
                assert stored[name] is None if math.isnan(value) else stored[name] == value
        counts = {model.__tablename__: await db.scalar(select(func.count()).select_from(model))
                  for model in [Company, Invoice, Payment, PaymentEvent, DatasetImport, OfflineEvaluation]}
        assert await db.scalar(select(func.count(Company.id)).where(Company.segment != CompanySegment.UNKNOWN)) == 0
        assert await db.scalar(select(func.count(func.distinct(Invoice.invoice_number)))) == len(frame)
        print('PERSISTED_SOURCE_RECONCILIATION:', json.dumps(result, sort_keys=True))
        print('DATABASE_ROW_COUNTS:', json.dumps(counts, sort_keys=True))
        print('PERSISTED_INTEGRITY:', json.dumps({'source_sha256': imported.source_sha256,
            'model_version': contract['version'], 'artifact_sha256': contract['artifact_sha256'],
            'preserved_source_date_records': len(invoices), 'valid_shap_reconstructions': len(invoices),
            'maximum_numeric_storage_rounding_error': max_error, 'stored_runtime_feature_parity_records': 500,
            'unknown_segments': counts['companies'], 'source_amount_sum': str(sum(frame.Amount, Decimal('0')))}, sort_keys=True))
    os.environ['PUBLIC_READ_ONLY'] = 'true'
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://isolated-test') as client:
        for path in ['/dashboard/calendar', '/analytics/metadata',
                     '/dashboard/analytics?start_date=2015-09-18&end_date=2016-03-17',
                     '/ml/offline-evaluations', '/health/ready']:
            response = await client.get(path)
            assert response.status_code == 200, response.text
            print('DATABASE_BACKED_API:', path, response.text)
        denied = await client.post('/recovery-cases/detect-overdue')
        assert denied.status_code == 403
        print('PUBLIC_MUTATION_DENIAL:', denied.status_code, denied.text)
    await engine.dispose()
    print('PASS: real PostgreSQL source counts, dates, feature snapshots, prediction provenance and SHAP queried back')


if __name__ == '__main__':
    asyncio.run(main())
