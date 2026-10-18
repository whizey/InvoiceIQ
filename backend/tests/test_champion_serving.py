import json
from datetime import date

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ml_intelligence import router
from app.core.json_values import json_value
from app.ml import champion_serving
from app.ml.payment_features import FEATURES
from app.ml.payment_risk_model import PaymentRiskArtifactError, predict_payment_risk
from app.services.payment_risk_context import PriorInvoiceSnapshot, compute_payment_risk_features


def cold_start():
    return compute_payment_risk_features(amount=1200, posting_date=date(2026, 1, 1), payment_term=30, history=[])


def test_real_champion_cold_start_and_feature_alignment():
    prediction = predict_payment_risk(cold_start(), "b2b_v2")
    contract = champion_serving.verified_contract()
    assert 0 <= prediction.late_payment_probability <= 1
    assert prediction.model_version == contract["version"]
    assert {item.feature for item in prediction.contributions} == set(contract["metadata"]["features"])
    assert contract["policy"]["serving_mode"] == "raw"
    assert len(prediction.contributions) == 14


def test_tampered_champion_rejected_before_deserialization(tmp_path, monkeypatch):
    monkeypatch.setattr(champion_serving, "ROOT", tmp_path)
    (tmp_path / "runtime_integrity.json").write_text(json.dumps({"schema_version": 1, "files": {}}))
    monkeypatch.setattr(champion_serving.joblib, "load", lambda _: pytest.fail("Must verify before loading"))
    with pytest.raises(PaymentRiskArtifactError):
        champion_serving.verified_contract()


def test_numpy_json_boundary():
    result = json_value({"count": np.int64(3), "ok": np.bool_(True),
                         "curve": np.array([np.float32(.5), np.nan]), "missing": np.inf})
    assert json.loads(json.dumps(result, allow_nan=False)) == {
        "count": 3, "ok": True, "curve": [.5, None], "missing": None,
    }


def test_strict_history_preserved_separately_from_weekend_adjusted_target():
    history = [PriorInvoiceSnapshot("A", 1000, date(2025, 10, 1), date(2025, 11, 1), date(2025, 11, 3))]
    features = compute_payment_risk_features(amount=1200, posting_date=date(2026, 1, 1), payment_term=30,
                                            history=history)
    assert features.historical_late_rate == 1
    assert features.historical_avg_days_late == 2


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("PAYMENT_RISK_MODEL", raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_default_catalog_and_prediction_api(client):
    catalog = client.get("/ml/models").json()
    assert catalog["primary_model"] == "b2b_v2"
    assert catalog["models"][0]["available"] is True
    features = json_value(cold_start().to_row())
    response = client.post("/ml/payment-risk/predict", json={"features": features})
    assert response.status_code == 200
    assert response.json()["calibration_status"] == "SUPPORTED_UNCALIBRATED"
    assert response.json()["explanation_units"] == "log_odds"


@pytest.mark.parametrize("bad", [True, "10", float("inf")])
def test_feature_contract_rejects_coercion(client, bad):
    from app.api.ml_intelligence import PaymentRiskRequest
    from pydantic import ValidationError
    features = {name: 0 for name in FEATURES}
    features["amount"] = bad
    with pytest.raises(ValidationError):
        PaymentRiskRequest(features=features)


def test_model_disabled_and_missing_features(client, monkeypatch):
    assert client.post("/ml/payment-risk/predict", json={"features": {"amount": 1}}).status_code == 422
    monkeypatch.setenv("PAYMENT_RISK_MODEL", "")
    assert client.get("/ml/models").json()["models"][0]["available"] is False
    assert client.post("/ml/payment-risk/predict", json={"features": json_value(cold_start().to_row())}).status_code == 503
