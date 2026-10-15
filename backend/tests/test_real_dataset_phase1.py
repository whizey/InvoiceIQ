"""Real-source verification; never connect to or clear the development database."""
import math
from decimal import Decimal
import os

import numpy as np
import pytest

from app.ml.champion_serving import champion_predict_many, verified_contract
from app.ml.datasets.build_primary_features import STRICT_TARGET_COLUMN, build_customer_history, repair_feature_scales
from app.ml.payment_features import NUMERIC_FEATURES
from app.seed.load_real_dataset import (
    DATASET, dataset_metadata, identity, persist_batch, read_dataset,
    require_isolated_url, resolve_dataset_path,
)
from app.models import Invoice
from app.services.payment_risk_context import PriorInvoiceSnapshot, compute_payment_risk_features


@pytest.fixture(scope="module")
def source():
    return read_dataset()


def test_local_path_override_and_missing_error(tmp_path, monkeypatch):
    assert resolve_dataset_path() == DATASET.resolve()
    replacement = tmp_path / "input.csv"
    replacement.write_text("source")
    monkeypatch.setenv("REAL_DATASET_PATH", str(replacement))
    assert resolve_dataset_path() == replacement
    with pytest.raises(FileNotFoundError, match="REAL_DATASET_PATH"):
        resolve_dataset_path(tmp_path / "missing.csv")


def test_real_source_exact_reconciliation(source):
    result = dataset_metadata(source)
    assert result == {"dataset_as_of": "2016-03-17", "row_count": 45839,
                      "customer_count": 69, "amount_total": "816262430", "outstanding_at_as_of": 0}
    assert len({identity("invoice", v) for v in source.Document_No}) == len(source)
    assert all(isinstance(v, Decimal) for v in source.Amount)
    # A contractual due date after the observation cutoff must not move 'today'.
    assert source.Net_Due_Date.max().date().isoformat() == "2016-04-03"


def test_database_guard_never_accepts_development():
    with pytest.raises(ValueError, match="dedicated"):
        require_isolated_url("postgresql+asyncpg://localhost/invoiceiq")
    assert require_isolated_url("postgresql+asyncpg://localhost/invoiceiq_public")


def test_500_real_posting_time_features_equal_frozen_offline_builder(source):
    raw = source.rename(columns={c: c.lower() for c in source.columns}).copy()
    raw["amount"] = raw.amount.astype(float)
    raw["days_late_strict"] = (raw.clearing_date - raw.net_due_date).dt.days
    raw[STRICT_TARGET_COLUMN] = (raw.days_late_strict > 0).astype(int)
    offline = repair_feature_scales(build_customer_history(raw)).set_index("document_no")
    groups = {customer: list(group.itertuples(index=False)) for customer, group in source.groupby("Cust_Num")}
    sampled = source.sample(n=500, random_state=45839)
    for row in sampled.itertuples(index=False):
        history = [PriorInvoiceSnapshot(str(prior.Document_No), float(prior.Amount),
            prior.Posting_Date.date(), prior.Net_Due_Date.date(), prior.Clearing_date.date())
            for prior in groups[row.Cust_Num] if prior.Posting_Date < row.Posting_Date]
        online = compute_payment_risk_features(amount=float(row.Amount), posting_date=row.Posting_Date.date(),
            payment_term=int(row.Payment_Term), history=history).to_row()
        for feature in NUMERIC_FEATURES:
            expected = float(offline.loc[row.Document_No, feature])
            assert math.isnan(online[feature]) if math.isnan(expected) else online[feature] == pytest.approx(expected, rel=1e-10, abs=1e-10), feature


def test_real_shap_reconstructs_prediction_and_persistence(source):
    features = [compute_payment_risk_features(amount=float(row.Amount), posting_date=row.Posting_Date.date(),
        payment_term=int(row.Payment_Term), history=[]) for row in source.head(10).itertuples(index=False)]
    contract = verified_contract()
    predictions = champion_predict_many(features)
    assert len(predictions) == 10
    for prediction in predictions:
        assert prediction.artifact_sha256 == contract["artifact_sha256"]
        margin = prediction.explanation_base_value + sum(c.shap_value for c in prediction.contributions)
        assert 1 / (1 + np.exp(-margin)) == pytest.approx(prediction.late_payment_probability, abs=1e-7)
    invoices = [Invoice() for _ in features]
    persist_batch(list(zip(invoices, features)), contract)
    for invoice, prediction in zip(invoices, predictions):
        assert invoice.payment_risk_artifact_sha256 == prediction.artifact_sha256
        assert invoice.payment_risk_explanation_metadata["units"] == "log_odds"
        assert len(invoice.payment_risk_drivers) == 14
        assert invoice.payment_risk_scored_at.tzinfo is not None


def test_binary_hash_mismatch_fails_before_deserialization(monkeypatch):
    from app.ml import champion_serving
    from app.ml.payment_risk_model import PaymentRiskArtifactError
    monkeypatch.setattr(champion_serving, "sha256_file", lambda _: "0" * 64)
    monkeypatch.setattr(champion_serving.joblib, "load", lambda _: pytest.fail("Untrusted binary must not be loaded"))
    with pytest.raises(PaymentRiskArtifactError, match="integrity"):
        champion_serving.verified_contract()


def test_offline_bpi_provenance_preserves_source_metrics():
    import hashlib
    import json
    from app.seed.offline_metrics import ARTIFACTS, bpi_evaluations
    records = list(bpi_evaluations())
    assert len(records) == 2
    for record in records:
        assert record['model_family'] == 'bpi_process_survival'
        assert record['evaluation_context'].startswith('offline_temporal')
        assert record['source_sha256'] == hashlib.sha256((ARTIFACTS / record['source']).read_bytes()).hexdigest()
    assert records[0]['metrics'] == json.loads((ARTIFACTS / records[0]['source']).read_text())


@pytest.mark.asyncio
async def test_postgres_import_idempotency_scoring_and_historical_workflow(source, monkeypatch, capsys):
    """Opt-in dedicated migrated DB only. Never seed/reset any existing database."""
    from sqlalchemy import select, func
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models import Company, Payment, PaymentEvent, RecoveryCase
    from app.seed.load_real_dataset import load, reconcile
    url = os.getenv('PHASE1_TEST_DATABASE_URL')
    if not url:
        pytest.skip('PHASE1_TEST_DATABASE_URL for a reachable migrated *_test database is not configured')
    require_isolated_url(url)
    from sqlalchemy.engine import make_url
    assert make_url(url).database.endswith('_test')
    monkeypatch.setenv('REAL_DATASET_DATABASE_URL', url)
    await load()
    first = capsys.readouterr().out
    await load()
    second = capsys.readouterr().out
    assert "'new_predictions': 0" in second
    assert "'cases_created': 0" in first and "'cases_created': 0" in second
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            result = await reconcile(session, source, verified_contract())
            assert result['scored_invoices'] == result['matching_artifact_hashes'] == 45839
            assert result['database_amount_total'] == '816262430.00'
            assert await session.scalar(select(func.count(Company.id))) == 69
            assert await session.scalar(select(func.count(Payment.id))) == 45839
            assert await session.scalar(select(func.count(PaymentEvent.id))) == 45839
            assert await session.scalar(select(func.count(RecoveryCase.id))) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_public_dashboard_real_authorization_and_transaction_order(monkeypatch):
    """Use the production get_db dependency: public auth must share its snapshot."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from httpx import ASGITransport, AsyncClient
    from app.core import db
    from app.main import app
    url = os.getenv('PHASE1_TEST_DATABASE_URL')
    if not url:
        pytest.skip('PHASE1_TEST_DATABASE_URL for the reconciled isolated corpus is absent')
    require_isolated_url(url)
    test_engine = create_async_engine(url)
    monkeypatch.setattr(db, 'async_session_factory', async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setenv('PUBLIC_READ_ONLY', 'true')
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/dashboard/analytics', params={
                'start_date': '2011-01-01', 'end_date': '2016-03-17'})
            assert response.status_code == 200, response.text
            assert response.json()['summary']['invoice_count'] == 45839
            assert (await client.post('/recovery-cases/detect-overdue')).status_code == 403
    finally:
        await test_engine.dispose()
