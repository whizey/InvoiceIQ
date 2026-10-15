"""Verify a historical, source-derived replay without customer-facing tools.

Requires a fresh isolated *_replay_test database, the reconciled source database,
and TEST_KAFKA_BOOTSTRAP_SERVERS / TEST_REDIS_URL. Never resets databases.
"""
import asyncio
from datetime import date
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiokafka import AIOKafkaConsumer
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.core.time import historical_as_of
from app.events.kafka_publisher import KafkaEventPublisher
from app.events.outbox import dispatch_batch
from app.events.topics import ALL_TOPICS
from app.main import app
from app.models import AgentDecision, AuditLog, OutboxEvent, RecoveryCase
from app.models.enums import AuditActor
from app.seed.historical_replay import reconstruct
from app.services.risk_engine import run_detection


async def main():
    cutoff = date(2015, 2, 3)
    identity = await reconstruct(cutoff)
    print('HISTORICAL_SOURCE_RECONCILIATION:', json.dumps(identity, sort_keys=True))
    invoice_ids = {UUID(value) for value in identity['selected_invoice_ids']}
    settings.redis_url = os.environ['TEST_REDIS_URL']
    token = secrets.token_urlsafe(32)
    os.environ['REVIEWER_API_KEYS'] = json.dumps({'isolated-phase1-reviewer': token})
    with historical_as_of(cutoff):
        async with async_session_factory() as db:
            detection = await run_detection(db, today=cutoff, invoice_ids=invoice_ids)
            assert len(detection.cases_created) == 3
            cases = (await db.scalars(select(RecoveryCase).where(RecoveryCase.invoice_id.in_(invoice_ids)))).all()
            by_invoice = {str(case.invoice_id): str(case.id) for case in cases}
            for case in cases:
                db.add(AuditLog(recovery_case_id=case.id, entity_type='recovery_case', entity_id=case.id,
                    event_type='HISTORICAL_SIMULATION_CONTEXT', actor=AuditActor.SYSTEM,
                    description='Simulated workflow on real historical invoices; no customer communication sent.',
                    metadata_={'historical_replay_as_of': cutoff.isoformat(),
                        'source_dataset_as_of': identity['source_dataset_as_of'],
                        'prediction_context': 'retrospective posting-time inference, not historical model deployment'}))
            await db.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://isolated-test') as client:
            details = []
            for invoice_id in identity['selected_invoice_ids']:
                case_id = by_invoice[invoice_id]
                response = await client.post(f'/recovery-cases/{case_id}/run', headers={'Idempotency-Key': 'historical-first-cycle'})
                assert response.status_code == 200, response.text
                details.append(response.json())
            held = [action for action in details[0]['actions'] if action['status'] == 'PENDING_APPROVAL']
            assert len(held) == 1 and held[0]['executed_at'] is None
            assert details[0]['communication_logs'] == []
            metrics = await client.get('/dashboard/metrics')
            assert metrics.status_code == 200 and metrics.json()['pending_approvals'] == 1
            print('DASHBOARD_BEFORE_APPROVAL:', metrics.text)
            path = f"/recovery-cases/actions/{held[0]['id']}/approve"
            unauthorized = await client.post(path, json={})
            assert unauthorized.status_code == 401
            forbidden = await client.post(path, json={}, headers={'Authorization': 'Bearer invalid'})
            assert forbidden.status_code == 403
            approved = await client.post(path, json={}, headers={'Authorization': f'Bearer {token}'})
            assert approved.status_code == 200, approved.text
            duplicate = await client.post(path, json={}, headers={'Authorization': f'Bearer {token}'})
            assert duplicate.status_code == 409
            print('REVIEW_HTTP_STATUS:', {'unauthenticated': unauthorized.status_code, 'unauthorized': forbidden.status_code,
                'authorized_simulation': approved.status_code, 'duplicate': duplicate.status_code})
            ordinary = by_invoice[identity['selected_invoice_ids'][1]]
            second = await client.post(f'/recovery-cases/{ordinary}/run', headers={'Idempotency-Key': 'historical-second-cycle'})
            assert second.status_code == 200, second.text
            overridden = [a for a in second.json()['actions'] if a['recommended_action_type'] != a['action_type']]
            assert overridden and overridden[-1]['action_type'] == 'WAIT'
            again = await client.post(f'/recovery-cases/{ordinary}/run', headers={'Idempotency-Key': 'historical-second-cycle'})
            assert again.status_code == 200 and len(again.json()['actions']) == len(second.json()['actions'])
            print('POLICY_OVERRIDE:', json.dumps(overridden[-1], sort_keys=True))
            for case_id in by_invoice.values():
                detail = (await client.get(f'/recovery-cases/{case_id}')).json()
                assert all(log['status'] == 'SIMULATED' for log in detail['communication_logs'])
                assert all(a['result'].get('simulated') for a in detail['actions'] if a['status'] == 'EXECUTED')
                print('PERSISTED_WORKFLOW:', json.dumps({'case_id': case_id,
                    'actions': detail['actions'], 'communication_logs': detail['communication_logs'],
                    'audit_event_types': [a['event_type'] for a in detail['audit_logs']]}, sort_keys=True))
            for path in ['/dashboard/metrics', '/dashboard/policy-overrides']:
                response = await client.get(path)
                assert response.status_code == 200
                print('DATABASE_BACKED_API:', path, response.text)
        async with async_session_factory() as db:
            decisions = (await db.scalars(select(AgentDecision))).all()
            diagnoses = [d for d in decisions if 'sources' in d.output]
            assert len(diagnoses) == 4
            for diagnosis in diagnoses:
                investigation = diagnosis.input_context['customer']['read_only_investigation']
                assert investigation['mode'] == 'constrained_deterministic_nl2sql' and investigation['rows']
            recommendations = [d for d in decisions if 'execution_mode' in d.output]
            assert len(recommendations) == 4
            assert all(d.output['execution_mode'] == 'deterministic_fallback' for d in recommendations)
            print('LANGGRAPH_DATABASE_TOOLS:', {'investigations': len(diagnoses), 'structured_recommendations': len(recommendations),
                'recommendation_provenance': 'deterministic_fallback', 'genuine_llm_recording': 'BLOCKED: no authorized provider or verified recording'})
            events = (await db.scalars(select(OutboxEvent))).all()
            event_ids = {e.id for e in events}
            print('PERSISTED_AUDIT:', {'audit_logs': await db.scalar(select(func.count(AuditLog.id))), 'outbox_events': len(events)})
    # Real broker connection failure, real backoff, real Kafka acknowledgement.
    failing = KafkaEventPublisher('127.0.0.1:1')
    first_id = {next(iter(event_ids))}
    failed = await dispatch_batch(async_session_factory, failing, event_ids=first_id)
    await failing.close()
    assert failed == {'delivered': 0, 'failed': 1}
    await asyncio.sleep(2.1)
    publisher = KafkaEventPublisher(os.environ['TEST_KAFKA_BOOTSTRAP_SERVERS'])
    consumer = AIOKafkaConsumer(*ALL_TOPICS, bootstrap_servers=os.environ['TEST_KAFKA_BOOTSTRAP_SERVERS'],
        group_id=f'phase1-historical-{uuid4()}', auto_offset_reset='earliest', enable_auto_commit=False)
    try:
        await consumer.start()
        delivered = await dispatch_batch(async_session_factory, publisher, limit=100, event_ids=event_ids)
        assert delivered == {'delivered': len(event_ids), 'failed': 0}, delivered
        received = set()
        for _ in range(20):
            batches = await consumer.getmany(timeout_ms=1000)
            for batch in batches.values():
                for record in batch:
                    received.add(UUID(json.loads(record.value)['event_id']))
            if event_ids <= received:
                break
        assert event_ids <= received
        repeat = await dispatch_batch(async_session_factory, publisher, limit=100, event_ids=event_ids)
        assert repeat == {'delivered': 0, 'failed': 0}
        async with async_session_factory() as db:
            rows = (await db.scalars(select(OutboxEvent))).all()
            assert all(e.published_at and e.delivery_mode == 'kafka' for e in rows)
            assert next(e for e in rows if e.id in first_id).attempts == 2
        print('REAL_KAFKA_OUTBOX:', {'first_connection_failure': failed, 'retry': delivered,
            'consumer_observed_events': len(event_ids & received), 'repeated_dispatch': repeat})
    finally:
        await consumer.stop()
        await publisher.close()
        await engine.dispose()
    print('PASS: historical PostgreSQL/LangGraph/policy/review/simulated execution/Kafka/dashboard acceptance')


if __name__ == '__main__':
    asyncio.run(main())
