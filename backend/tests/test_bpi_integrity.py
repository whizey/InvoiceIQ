import json
from types import SimpleNamespace

import pytest
import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ml_intelligence import router
from app.ml import bpi_survival_serving


def test_real_bpi_registry_loads_verified_model():
    assert bpi_survival_serving.verified_model_version().startswith("bpi_survival_lightgbm_v1:")
    assert bpi_survival_serving.load_model().named_steps["model"].n_jobs == 1


def test_bpi_tamper_fails_before_deserialization(tmp_path, monkeypatch):
    root = tmp_path / "bpi"
    root.mkdir()
    (root / "runtime_integrity.json").write_text(json.dumps({"schema_version": 1, "files": {}}))
    monkeypatch.setattr(bpi_survival_serving, "MODEL_PATH", root / "models/hazard_model.joblib")
    monkeypatch.setattr(bpi_survival_serving, "joblib", SimpleNamespace(load=lambda _: pytest.fail("Unverified pickle must not load")))
    with pytest.raises(RuntimeError, match="verification failed"):
        bpi_survival_serving.load_model()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/ml/models").json()["models"][1]["available"] is False
    assert client.get("/ml/process-survival/features").status_code == 503


@pytest.mark.parametrize("values", [[.4, .3], [0, np.nan], [0, 1.1]])
def test_invalid_survival_curve_fails_closed(monkeypatch, values):
    monkeypatch.setattr(bpi_survival_serving, "load_model", lambda: object())
    monkeypatch.setattr(bpi_survival_serving, "predict_recovery_curves",
                        lambda *_: (None, None, pd.DataFrame([values], columns=[7., 30.])))
    with pytest.raises(RuntimeError, match="probability contract"):
        bpi_survival_serving.score_cases(pd.DataFrame([{"feature": 1}]))
