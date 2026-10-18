"""Model risk regressions; no training, provider calls, or database writes."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.ml.common import evaluate, known_outcome_mask, temporal_folds, to_numeric_codes
from app.ml.drift_monitor import compare_feature, population_stability_index
from app.ml.model_risk import classification_metrics, target_contract
from app.ml.payment_features import TARGET_COLUMN
from app.ml.train_b2b_v2 import add_targets


def test_metrics_exact_confusion_and_calibration():
    result = classification_metrics([0, 0, 1, 1], [0.1, 0.6, 0.4, 0.9])
    row = result["operational_thresholds"][1]
    assert row["confusion_matrix"] == [[1, 1], [1, 1]]
    assert row["precision"] == row["recall"] == row["f1"] == row["balanced_accuracy"] == .5
    assert result["brier_score"] == pytest.approx(.185)
    assert sum(bin_["rows"] for bin_ in result["reliability"]) == 4


@pytest.mark.parametrize("labels", [[0, 0], [1, 1]])
def test_single_class_metrics_are_json_safe(labels):
    result = classification_metrics(labels, [0, 1])
    assert result["roc_auc"] is None
    assert result["operational_thresholds"][1]["balanced_accuracy"] is None
    assert np.isfinite(result["log_loss"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("labels,p", [([], []), ([0], [np.nan]), ([1], [1.1]), ([2], [.5]), ([0, 1], [.5])])
def test_invalid_metric_inputs_rejected(labels, p):
    with pytest.raises(ValueError):
        classification_metrics(labels, p)


def test_common_single_class_evaluation_does_not_invent_auc():
    frame = pd.DataFrame({TARGET_COLUMN: [0, 0], "posting_date": pd.to_datetime(["2015-01-01", "2015-01-02"])})
    scored = evaluate(frame, np.array([.1, .2]))
    assert scored["pooled_roc_auc"] is scored["within_period_auc"] is scored["pooled_pr_auc"] is None
    json.dumps(scored, allow_nan=False)


@pytest.mark.parametrize("current", [[2] * 10, [0] * 10])
def test_constant_feature_drift_detects_both_directions(current):
    assert population_stability_index([1] * 10, current) > .2
    assert population_stability_index([1] * 10, [1] * 10) == 0


def test_missing_numeric_period_is_reported_without_fabricated_psi(tmp_path):
    baseline, recent = tmp_path / "baseline.csv", tmp_path / "recent.csv"
    baseline.write_text("amount\n1\n2\n")
    recent.write_text("amount\nnan\nnan\n")
    result = compare_feature(baseline, recent, "amount")
    assert result["psi"] is None and result["drift_warning"]
    assert result["missing_rate_change"] == 1


@pytest.mark.parametrize("values,bins", [([np.inf], 10), ([1], 1), ([1], True)])
def test_invalid_psi_parameters(values, bins):
    with pytest.raises(ValueError):
        population_stability_index(values, [1], bins)


def outcome_frame():
    return pd.DataFrame({"posting_date": pd.date_range("2015-01-01", periods=12),
        "net_due_date": pd.date_range("2015-01-01", periods=12),
        "days_late_strict": [100] + [0] * 11,
        TARGET_COLUMN: [1, 0] * 6})


def test_training_label_not_known_merely_because_invoice_was_posted():
    frame = outcome_frame()
    known = known_outcome_mask(frame, pd.Timestamp("2015-01-10"))
    assert not known.iloc[0]  # April clearing, January posting
    assert not known.iloc[9]  # same-day clearing is not yet observed
    assert known.iloc[1]
    for train, test in temporal_folds(frame):
        assert 0 not in train
        assert known_outcome_mask(frame.iloc[train], frame.posting_date.iloc[test].min()).all()


def test_outcome_availability_requires_evidence():
    with pytest.raises(ValueError, match="Outcome availability"):
        known_outcome_mask(pd.DataFrame({"posting_date": []}), pd.Timestamp("2015-01-10"))


def test_fold_imputation_uses_only_fold_training():
    train, later = pd.DataFrame({"amount": [1., 3.]}), pd.DataFrame({"amount": [np.nan, 10000.]})
    (_, prepared), medians = to_numeric_codes([train, later])
    assert medians["amount"] == 2 and prepared.amount.iloc[0] == 2


@pytest.mark.parametrize("delay,flag", [(np.nan, 0), (1, 0), (0, 2), (1.5, 1)])
def test_target_builder_rejects_unknown_and_inconsistent_outcomes(delay, flag):
    frame = pd.DataFrame({"posting_date": pd.to_datetime(["2015-01-01"]),
        "net_due_date": pd.to_datetime(["2015-01-02"]),
        "days_late_weekend_adjusted": [delay], TARGET_COLUMN: [flag]})
    with pytest.raises(ValueError):
        add_targets(frame)


def test_horizon_targets_and_availability_boundaries():
    frame = pd.DataFrame({"posting_date": pd.to_datetime(["2015-01-01"] * 4),
        "net_due_date": pd.to_datetime(["2015-01-03"] * 4),
        "days_late_weekend_adjusted": [0, 7, 8, 16], TARGET_COLUMN: [0, 1, 1, 1]})
    result = add_targets(frame)
    assert result.target_delay_over_7d.tolist() == [0, 0, 1, 1]
    assert result.target_delay_over_15d.tolist() == [0, 0, 0, 1]
    assert (result.available_delay_over_7d == pd.Timestamp("2015-01-12")).all()
    assert result.available_days_late.iloc[3] == pd.Timestamp("2015-01-22")
    assert target_contract()["prediction_time"].startswith("invoice posting")


def test_cached_legacy_contract_cannot_load_replaced_binary(monkeypatch, tmp_path):
    from app.ml import payment_risk_model as serving
    directory = tmp_path / "random_forest"
    directory.mkdir()
    (directory / "model.joblib").write_bytes(b"untrusted replacement")
    monkeypatch.setattr(serving, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(serving, "_load_contract", lambda _: {"model_file": "model.joblib", "model_sha256": "0" * 64})
    monkeypatch.setattr(serving.joblib, "load", lambda _: pytest.fail("Must not deserialize"))
    serving._load_model.cache_clear()
    with pytest.raises(serving.PaymentRiskArtifactError, match="before deserialization"):
        serving._load_model("random_forest")


def test_cached_legacy_contract_cannot_use_replaced_medians(monkeypatch, tmp_path):
    from app.ml import payment_risk_model as serving
    from app.ml.payment_features import FEATURES, PaymentRiskFeatures
    directory = tmp_path / "random_forest"
    directory.mkdir()
    (directory / "median_impute_values.json").write_text(json.dumps({name: 999 for name in FEATURES}))
    monkeypatch.setattr(serving, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(serving, "_load_contract", lambda _: {"category_levels": {},
        "median_file": "median_impute_values.json", "median_sha256": "0" * 64})
    with pytest.raises(serving.PaymentRiskArtifactError, match="before preprocessing"):
        serving._prepare_frames("random_forest", PaymentRiskFeatures(**{name: 0 for name in FEATURES}))


def _assert_model_risk_evidence(report):
    """Verify the report against real offline evaluation evidence."""
    import hashlib
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    audit_path = (
        root
        / "backend/app/ml/artifacts/b2b_v2"
        / "lightgbm_challenge/calibration_audit.json"
    )

    evidence = json.loads(audit_path.read_text())["late_payment"]
    reported = report["models"][0]

    assert reported["metrics"]["rows"] == evidence["raw"]["rows"]
    assert reported["metrics"]["roc_auc"] == evidence["raw"]["roc_auc"]
    assert reported["metrics"]["pr_auc"] == evidence["raw"]["pr_auc"]
    assert reported["calibration"]["platt_accepted"] == evidence["accepted"]

    actual_hash = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    assert report["evidence_sources"]["calibration"]["sha256"] == actual_hash

    explainability = report["explainability"]
    assert explainability["rows_checked"] is None
    assert explainability["status"] == "Not independently verified"


def test_frozen_report_counts_and_champion_identity():
    from app.ml.champion_serving import verified_contract
    root = Path(__file__).resolve().parents[2]
    report = json.loads((root / "docs/evaluations/model-risk-v1.json").read_text())
    assert report["models"][0]["version"] == verified_contract()["version"]
    _assert_model_risk_evidence(report)

    assert report["test_status"].find("not pristine") >= 0


def test_cohort_excluded_invoice_still_contributes_known_posting_history(monkeypatch, tmp_path):
    from app.ml.datasets import build_primary_features as builder
    raw = pd.DataFrame({"document_no": [1, 2, 3], "cust_num": [1, 1, 2],
        "posting_date": pd.to_datetime(["2016-01-01", "2016-01-03", "2015-11-01"]),
        "doc_date": pd.to_datetime(["2016-01-01", "2016-01-03", "2015-11-01"]),
        "net_due_date": pd.to_datetime(["2016-02-10", "2016-01-10", "2015-12-01"]),
        "clearing_date": pd.to_datetime(["2016-01-02", "2016-01-11", "2016-03-17"]),
        "amount": [100., 200., 300.], "payment_term": [40, 7, 30],
        "delayflag": [0, 1, 1], "payment_method_description": ["BANK"] * 3,
        "region": ["R"] * 3, "city": ["C"] * 3, "zipcode": ["Z"] * 3})
    monkeypatch.setattr(builder, "load_raw", lambda: raw.copy())
    monkeypatch.setattr(builder, "PROCESSED_DIR", tmp_path)
    monkeypatch.setattr(builder, "OUTPUT_PATH", tmp_path / "features.csv.gz")
    monkeypatch.setattr(builder, "MANIFEST_PATH", tmp_path / "manifest.json")
    result = builder.build().set_index("document_no")
    assert 1 not in result.index  # due+45 after extraction cutoff
    assert result.loc[2, "prior_resolved_invoice_count"] == 1
    assert result.loc[2, "historical_avg_invoice_amount"] == 100
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["target"]["positive_rate"] == float(result[TARGET_COLUMN].mean())


def test_model_risk_api_uses_offline_evidence_without_deserialization(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.ml_intelligence import router
    from app.ml import champion_serving
    monkeypatch.setattr(champion_serving.joblib, "load", lambda _: pytest.fail("Offline API must not load a binary"))
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/ml/model-risk")
    assert response.status_code == 200
    _assert_model_risk_evidence(response.json())


def test_model_risk_api_refuses_stale_champion_evidence(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.ml_intelligence import router
    from app.ml import champion_serving
    monkeypatch.setattr(champion_serving, "verified_contract", lambda: {"version": "changed"})
    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get("/ml/model-risk").status_code == 503
