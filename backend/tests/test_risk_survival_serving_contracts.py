"""Real serving and risk contract checks without loading customer data or external models."""
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.ml import bpi_conditional_survival as conditional
from app.ml import bpi_recovery_serving as recovery
from app.ml import bpi_survival_serving as serving
from app.ml import model_risk


CURVE = {7: 0.2, 30: 0.5, 60: 0.7, 90: 0.8, 120: 0.9}


def test_conditional_survival_at_receipt_and_partway_through():
    at_receipt = conditional.conditional_clearance_probabilities(
        CURVE, elapsed_days_since_receipt=0, horizons=(7, 30, 60, 90)
    )
    assert at_receipt["clearance_probability_next_7d"] == pytest.approx(0.2)
    assert at_receipt["clearance_probability_next_30d"] == pytest.approx(0.5)
    mid = conditional.conditional_clearance_probabilities(
        CURVE, elapsed_days_since_receipt=10, horizons=(7, 30, 60, 90)
    )
    assert all(0 <= value <= 1 for value in mid.values())
    assert mid["clearance_probability_next_7d"] < mid["clearance_probability_next_60d"]
    at_checkpoint = conditional.conditional_clearance_probabilities(
        CURVE, elapsed_days_since_receipt=30, horizons=(30, 90)
    )
    assert at_checkpoint["clearance_probability_next_30d"] == pytest.approx(0.4)


@pytest.mark.parametrize("curve", [
    {}, {0: 0.1, 7: 0.2}, {-1: 0.2, 30: 0.5},
    {7: -0.1, 30: 0.5}, {7: 0.2, 30: 1.1},
    {7: 0.6, 30: 0.5}, {7: float("nan"), 30: 0.5},
    {7: 0.2, float("inf"): 0.9},
])
def test_conditional_survival_refuses_invalid_curves(curve):
    with pytest.raises(ValueError):
        conditional.conditional_clearance_probabilities(
            curve, elapsed_days_since_receipt=0, horizons=(7,)
        )


@pytest.mark.parametrize("age,horizons", [
    (-1, (7,)), (float("nan"), (7,)),
    (float("inf"), (7,)), (31, (90,)),
    (0, ()), (0, (True,)), (0, (0,)), (0, (1.0,)),
])
def test_conditional_survival_refuses_invalid_ages_and_horizons(age, horizons):
    with pytest.raises(ValueError):
        conditional.conditional_clearance_probabilities(
            CURVE, elapsed_days_since_receipt=age, horizons=horizons
        )


def test_conditional_survival_refuses_near_certain_prior_clearance():
    with pytest.raises(ValueError, match="near-zero"):
        conditional.conditional_clearance_probabilities(
            {7: 1.0, 30: 1.0, 90: 1.0},
            elapsed_days_since_receipt=7, horizons=(30,)
        )


def test_recovery_policy_gets_real_scoring_fields_and_outputs(monkeypatch):
    scored = pd.DataFrame([{
        "clearance_probability_7d": 0.1,
        "clearance_probability_30d": 0.3,
        "clearance_probability_60d": 0.6,
        "clearance_probability_90d": 0.8,
        "model_version": "test-version",
    }])
    calls = []
    monkeypatch.setattr(recovery, "score_cases", lambda frame: scored)
    def decide(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"action": "ESCALATE_CASE"})
    monkeypatch.setattr(recovery, "decide_recovery_action", decide)
    result = recovery.score_case_with_policy(
        pd.DataFrame([{"case_id": "one"}]),
        amount_outstanding=25000, days_overdue=14,
        reminders_sent=2, promise_to_pay_active=True,
    )
    assert result["survival"]["model_version"] == "test-version"
    assert result["decision"] == {"action": "ESCALATE_CASE"}
    assert calls == [{
        "clearance_probability_7d": 0.1,
        "clearance_probability_30d": 0.3,
        "clearance_probability_60d": 0.6,
        "clearance_probability_90d": 0.8,
        "amount_outstanding": 25000, "days_overdue": 14,
        "reminders_sent": 2, "promise_to_pay_active": True,
    }]


def test_recovery_policy_rejects_batch_and_empty_scores(monkeypatch):
    with pytest.raises(ValueError, match="exactly one"):
        recovery.score_case_with_policy(pd.DataFrame({"case_id": [1, 2]}),
            amount_outstanding=1, days_overdue=1)
    with pytest.raises(ValueError, match="exactly one"):
        recovery.score_case_with_policy(pd.DataFrame({"case_id": []}),
            amount_outstanding=1, days_overdue=1)
    monkeypatch.setattr(recovery, "score_cases", lambda _: pd.DataFrame())
    with pytest.raises(RuntimeError, match="no score"):
        recovery.score_case_with_policy(pd.DataFrame({"case_id": [1]}),
            amount_outstanding=1, days_overdue=1)


def test_verified_survival_model_contract_and_corruption(tmp_path, monkeypatch):
    model_path = tmp_path / "models" / "hazard_model.joblib"
    model_path.parent.mkdir()
    model_path.write_bytes(b"isolated-placeholder-do-not-deserialize")
    contract_path = tmp_path / "serving_contract.json"
    contract_path.write_text(json.dumps({
        "champion": "lightgbm_hazard",
        "serving_horizons_days": [7, 30, 60, 90],
    }))
    files = {
        "models/hazard_model.joblib": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "serving_contract.json": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
    }
    (tmp_path / "runtime_integrity.json").write_text(
        json.dumps({"schema_version": 1, "files": files})
    )
    monkeypatch.setattr(serving, "MODEL_PATH", model_path)
    verified = serving.verified_model_version()
    assert verified.startswith(serving.MODEL_VERSION + ":")
    model_path.write_bytes(b"changed-without-authorization")
    with pytest.raises(RuntimeError, match="verification failed"):
        serving.verified_model_version()
    model_path.write_bytes(b"isolated-placeholder-do-not-deserialize")
    contract_path.write_text(json.dumps({
        "champion": "unknown",
        "serving_horizons_days": [7, 30, 60, 90],
    }))
    with pytest.raises(RuntimeError, match="verification failed"):
        serving.verified_model_version()


def test_survival_scores_are_monotone_and_preserve_input_order(monkeypatch):
    monkeypatch.setattr(serving, "load_model", lambda: object())
    monkeypatch.setattr(serving, "verified_model_version", lambda: "verified-test")
    columns = [float(end) for _, end in serving.INTERVALS]
    curve = pd.DataFrame([
        {day: min(1, day / 120) for day in columns},
        {day: min(1, day / 200) for day in columns},
    ])
    monkeypatch.setattr(serving, "predict_recovery_curves",
                        lambda model, rows: (None, None, curve))
    cases = pd.DataFrame({"case_id": ["a", "b"]}, index=[10, 20])
    result = serving.score_cases(cases, include_recovery_curve=True)
    assert list(result.model_version) == ["verified-test", "verified-test"]
    assert result.loc[0, "likely_clearance_window_days"] == 60
    assert result.loc[1, "likely_clearance_window_days"] == 120
    assert result.loc[0, "clearance_probability_30d"] == pytest.approx(0.25)
    assert result.loc[1, "clearance_probability_90d"] == pytest.approx(0.45)
    assert result.loc[0, "recovery_curve"]["7"] == pytest.approx(7 / 120)
    assert serving.score_cases(pd.DataFrame()).empty


@pytest.mark.parametrize("values", [
    [[0.2, 0.1, 0.3]], [[-0.1, 0.2, 0.3]],
    [[0.2, 1.1, 1.0]], [[0.2, float("nan"), 0.9]],
])
def test_survival_scoring_rejects_invalid_probability_curves(monkeypatch, values):
    monkeypatch.setattr(serving, "load_model", lambda: object())
    monkeypatch.setattr(serving, "predict_recovery_curves",
                        lambda model, rows: (None, None, pd.DataFrame(values)))
    with pytest.raises(RuntimeError, match="probability contract"):
        serving.score_cases(pd.DataFrame({"case_id": ["one"]}))


def test_survival_scoring_rejects_wrong_row_count(monkeypatch):
    monkeypatch.setattr(serving, "load_model", lambda: object())
    monkeypatch.setattr(serving, "predict_recovery_curves",
                        lambda model, rows: (None, None, pd.DataFrame([[0.1, 0.2]])))
    with pytest.raises(RuntimeError, match="probability contract"):
        serving.score_cases(pd.DataFrame({"case_id": ["one", "two"]}))


def test_risk_evaluation_both_classes_and_calibration():
    metrics = model_risk.classification_metrics(
        [0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9], bins=5,
        thresholds=(0.25, 0.5, 0.75),
    )
    assert metrics["rows"] == 4
    assert metrics["positive_rows"] == 2
    assert metrics["roc_auc"] == pytest.approx(1)
    assert metrics["pr_auc"] == pytest.approx(1)
    assert metrics["brier_score"] == pytest.approx(0.025)
    assert sum(bucket["rows"] for bucket in metrics["reliability"]) == 4
    assert metrics["ece"] > 0
    assert all(x["confusion_matrix"] == [[2, 0], [0, 2]]
               for x in metrics["operational_thresholds"])


def test_risk_evaluation_single_class_is_explicitly_undefined():
    result = model_risk.classification_metrics(
        [0, 0, 0], [0.1, 0.3, 0.5], thresholds=(0.5,)
    )
    assert result["discrimination_status"] == "single_class"
    assert result["roc_auc"] is None and result["pr_auc"] is None
    assert result["operational_thresholds"][0]["recall"] is None
    result = model_risk.classification_metrics(
        [1, 1], [0.0, 0.0], thresholds=(0.8,)
    )
    assert result["roc_auc"] is None
    assert result["operational_thresholds"][0]["precision"] is None


@pytest.mark.parametrize("y,p,bins", [
    ([], [], 10), ([0], [0, 1], 10), ([2], [0.3], 10),
    ([0, 1], [0.1, float("nan")], 10),
    ([0, 1], [0.1, 1.2], 10),
    ([0, 1], [0.1, 0.9], 1),
    ([0, 1], [0.1, 0.9], True),
])
def test_risk_evaluation_rejects_invalid_input(y, p, bins):
    with pytest.raises(ValueError):
        model_risk.classification_metrics(y, p, bins=bins)


def test_risk_evaluation_rejects_invalid_operational_threshold():
    with pytest.raises(ValueError, match="Threshold"):
        model_risk.classification_metrics([0, 1], [0.1, 0.9],
                                          thresholds=(1.1,))
    contract = model_risk.target_contract()
    assert contract["observation_cutoff"] == "2016-03-17"
    assert "future payments" in contract["excluded_inputs"]
    assert contract["feature_availability"]
