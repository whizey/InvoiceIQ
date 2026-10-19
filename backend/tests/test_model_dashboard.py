"""Dashboard authorization and aggregate query contracts; no database writes."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.ml_intelligence import router
from app.core.analytics_auth import AnalyticsIdentity
from app.core.db import get_db
from app.services.model_dashboard import live_evidence, research_evidence


def test_dashboard_requires_scoped_credentials(monkeypatch):
    monkeypatch.setenv('PUBLIC_READ_ONLY', 'false')
    monkeypatch.setenv('ANALYTICS_API_KEYS', json.dumps({'test': {'token': 'dashboard-test-token', 'company_ids': [str(uuid4())]}}))
    app = FastAPI()
    app.include_router(router)
    db = AsyncMock()
    async def fake_db():
        yield db
    app.dependency_overrides[get_db] = fake_db
    with TestClient(app) as client:
        assert client.get('/ml/dashboard').status_code == 401
        assert client.get('/ml/dashboard', headers={'Authorization': 'Bearer wrong'}).status_code == 403
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_dashboard_scopes_every_observation_and_never_infers_telemetry():
    company = uuid4()
    queries = []
    returns = iter([
        SimpleNamespace(one=lambda: (0, 0, 0, 0, None, 0)),
        SimpleNamespace(one=lambda: (0, 0, None, None)),
        SimpleNamespace(all=lambda: []),
    ])
    async def execute(query):
        queries.append(query)
        return next(returns)
    db = SimpleNamespace(execute=execute, scalar=AsyncMock(side_effect=[None, 0, 0]))
    result = await live_evidence(db, AnalyticsIdentity('workspace-test', (company,)))
    assert result['coverage']['eligible'] == result['coverage']['scored'] == 0
    assert result['health']['requests'] is result['health']['p95_ms'] is None
    assert result['settled_count'] == 0
    assert all(b['late_rate'] is None for b in result['bands'])
    for query in queries + [call.args[0] for call in db.scalar.await_args_list[1:]]:
        sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
        assert str(company) in sql
        assert 'company_id' in sql
    outcome_sql = str(queries[2].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
    assert 'payments.currency = invoices.currency' in outcome_sql
    assert 'sum(payments.amount) OVER' in outcome_sql
    assert 'invoices.amount_paid' in outcome_sql


@pytest.mark.asyncio
async def test_recorded_health_and_band_boundaries_are_reported_without_invented_reasons():
    returns = iter([
        SimpleNamespace(one=lambda: (10, 8, 4, 3, datetime(2026, 1, 1, tzinfo=timezone.utc), 6)),
        SimpleNamespace(one=lambda: (5, 1, 12.5, 50.0)),
        SimpleNamespace(all=lambda: [(0, 2, 1), (4, 1, 1)]),
    ])
    async def execute(query):
        return next(returns)
    db = SimpleNamespace(execute=execute, scalar=AsyncMock(side_effect=[None, 2, 1]))
    data = await live_evidence(db, AnalyticsIdentity('test', (uuid4(),)))
    assert data['coverage']['unscored'] == 2
    assert data['coverage']['reasons'] is None
    assert data['health']['requests'] == 5
    assert data['health']['failures'] == 1
    assert data['health']['complete_interval'] is False
    assert data['bands'][0] == {'count': 2, 'late_rate': .5}
    assert data['bands'][4] == {'count': 1, 'late_rate': 1}
    assert data['settled_count'] == 3


def test_calibration_uses_the_correct_champion_audit_and_discloses_test_history():
    data = research_evidence()
    by_target = {row['target']: row for row in data['targets']}
    assert by_target['late_payment']['raw']['roc_auc'] == pytest.approx(.7136343064083855)
    assert by_target['delay_over_7d']['calibrated']['roc_auc'] == pytest.approx(.6262562123411181)
    assert by_target['days_late_p80']['validation']['median_pinball'] > 0
    disclosure = data['test_disclosure'].lower()
    assert 'not pristine held-out test evidence' in disclosure
    assert 'public dataset' in disclosure
    assert 'examined during prior development' in disclosure
    assert data['bpi']['curve'] is None
    assert data['bpi']['receipt_never_cleared'] is None
