import os
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core import db
from app.main import app
from app.models import Invoice, InferenceRecord
from app.services.persisted_predictions import stored_prediction_report
from app.ml.champion_serving import verified_contract


@pytest.mark.asyncio
async def test_real_imported_predictions_report_without_fabricated_telemetry(monkeypatch):
    url = os.getenv('PHASE1_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Isolated reconciled source database is required')
    test_engine = create_async_engine(url)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(db, 'async_session_factory', factory)
    monkeypatch.setenv('PUBLIC_READ_ONLY', 'true')
    try:
        async with factory() as session:
            before = await session.scalar(select(func.count()).select_from(InferenceRecord))
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            query = {'start_date': '2011-01-01', 'end_date': '2016-03-17', 'view': 'ml'}
            dashboard = (await client.get('/dashboard/analytics', params=query)).json()
            ml = await client.get('/ml/stored-predictions', params={k:v for k,v in query.items() if k!='view'})
            assert ml.status_code == 200, ml.text
            assert dashboard['stored_predictions'] == ml.json()['stored_predictions'] == 45839
            assert dashboard['model_versions'] == ml.json()['model_versions'] == {verified_contract()['version']:45839}
            assert sum(v for k,v in dashboard['risk_distribution'].items() if k!='UNSCORED') == 45839
            assert dashboard['risk_distribution'] == {'HIGH':13523,'MEDIUM':2559,'LOW':29757}
            assert ml.json()['without_recorded_inference'] == 45839
            assert dashboard['inference_total'] == 0 and dashboard['inference_latency_ms'] is None
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(InferenceRecord)) == before == 0
            # A corrupted owned ORM snapshot must not be reported as trusted.
            invoice = await session.scalar(select(Invoice).limit(1))
            original = invoice.payment_risk_artifact_sha256
            invoice.payment_risk_artifact_sha256 = '0'*64
            await session.flush()
            report = await stored_prediction_report(session, company_ids=[invoice.company_id], currency='INR',
                start_date=date(2011,1,1), end_date=date(2016,3,17))
            invoice.payment_risk_artifact_sha256 = original
            await session.flush()
            restored = await stored_prediction_report(session, company_ids=[invoice.company_id], currency='INR',
                start_date=date(2011,1,1), end_date=date(2016,3,17))
            assert restored['stored_predictions'] == report['stored_predictions'] + 1
            await session.rollback()
    finally:
        await test_engine.dispose()


def test_simulated_execution_description_never_claims_delivery():
    from types import SimpleNamespace
    from app.agents.graph import _describe_execution
    from app.models.enums import RecoveryActionType
    for action in [RecoveryActionType.SEND_EMAIL, RecoveryActionType.SEND_PAYMENT_LINK]:
        text = _describe_execution(action, SimpleNamespace(invoice_number='test-only'), {'simulated':True})
        assert 'Simulated' in text
        assert 'email sent' not in text.lower() and 'delivered' not in text.lower()


@pytest.mark.asyncio
async def test_replay_cannot_reach_email_provider(monkeypatch):
    from app.core.time import historical_as_of
    from app.tools.email_provider import send_reminder_email
    with historical_as_of(date(2015,2,3)), pytest.raises(RuntimeError, match='disabled during historical replay'):
        await send_reminder_email(None,None,None,None,'unused')


@pytest.mark.asyncio
async def test_legacy_simulated_audit_projection_preserves_stored_history(monkeypatch):
    from sqlalchemy.engine import make_url
    from app.models import RecoveryCase, AuditLog
    url = os.getenv('PHASE1_REPLAY_DATABASE_URL')
    if not url:
        pytest.skip('Isolated historical replay database is required')
    replay = make_url(url)
    assert replay.database == 'invoiceiq_phase1_replay_test'
    test_engine = create_async_engine(replay)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(db,'async_session_factory',factory)
    monkeypatch.setenv('PUBLIC_READ_ONLY','false')
    monkeypatch.setenv('ANALYTICS_API_KEYS','')
    try:
        async with factory() as session:
            ids = list((await session.scalars(select(RecoveryCase.id))).all())
            before = {str(row.id):row.description for row in (await session.scalars(select(AuditLog))).all()}
        assert len(ids)==3
        normalized = 0
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            for case_id in ids:
                response = await client.get(f'/recovery-cases/{case_id}')
                assert response.status_code==200,response.text
                for audit in response.json()['audit_logs']:
                    if audit.get('display_event_type')=='COMMUNICATION_SIMULATED':
                        assert audit['description'].startswith('Simulated communication — not sent.')
                        assert audit['original_description']==before[audit['id']]
                        normalized+=1
        assert normalized==2
        async with factory() as session:
            after = {str(row.id):row.description for row in (await session.scalars(select(AuditLog))).all()}
            assert after==before
    finally:
        await test_engine.dispose()
