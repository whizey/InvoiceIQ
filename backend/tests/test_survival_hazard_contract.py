"""Deterministic survival-model contract checks; no large BPI download required."""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.ml import train_bpi_survival as survival


def cases():
    receipt = pd.to_datetime([
        "2018-01-01T00:00:00Z",
        "2018-01-04T00:00:00Z",
        "2018-01-06T00:00:00Z",
        "2018-01-07T00:00:00Z",
    ], utc=True)
    return pd.DataFrame({
        "case_id": ["paid", "late", "open", "future"],
        "invoice_timestamp": receipt,
        "clear_timestamp": pd.to_datetime([
            "2018-01-03T00:00:00Z", "2018-02-11T00:00:00Z",
            None, "2018-03-15T00:00:00Z",
        ], utc=True),
        "event_observed": [1, 1, 0, 1],
        "invoice_amount_eur": [100, 250, 500, 800],
        "company": ["A", "A", "B", "B"],
    })


def test_cutoff_is_timezone_aware_and_does_not_leak_future_payments():
    data = cases()
    view = survival.recensor_training_data(data, "2018-02-01")
    assert set(view.case_id) == set(data.case_id)
    by_id = view.set_index("case_id")
    assert by_id.loc["paid", "_fold_event"] == 1
    assert by_id.loc["paid", "_fold_duration"] == pytest.approx(2)
    assert by_id.loc["late", "_fold_event"] == 0
    assert by_id.loc["late", "_fold_duration"] == pytest.approx(28)
    assert by_id.loc["future", "_fold_event"] == 0
    assert by_id.loc["open", "_fold_event"] == 0
    assert survival.utc(datetime(2018, 2, 1, tzinfo=timezone.utc)).tzinfo is not None
    malformed = data.copy()
    malformed.loc[0, "invoice_timestamp"] = pd.Timestamp("2018-03-01", tz="UTC")
    assert "paid" not in set(survival.recensor_training_data(malformed, "2018-02-01").case_id)


def test_discrete_hazard_expansion_includes_only_observed_at_risk_windows():
    data = cases().assign(_fold_duration=[2, 38, 29, 66], _fold_event=[1, 1, 0, 1])
    hazard = survival.expand_hazard_rows(data, "_fold_duration", "_fold_event")
    paid = hazard.loc[hazard.case_id == "paid"]
    late = hazard.loc[hazard.case_id == "late"]
    open_ = hazard.loc[hazard.case_id == "open"]
    future = hazard.loc[hazard.case_id == "future"]
    assert len(paid) == 1 and paid.hazard_event.tolist() == [1]
    assert late.hazard_event.tolist() == [0, 0, 0, 1]
    assert open_.hazard_event.tolist() == [0, 0]
    assert future.hazard_event.sum() == 1
    assert future.interval_end_days.max() == 90
    assert survival.expand_hazard_rows(data.assign(_fold_duration=-1), "_fold_duration", "_fold_event").empty


def test_feature_preprocessor_and_two_model_factories():
    data = cases()
    expanded = survival.prediction_grid(data)
    assert len(expanded) == len(data) * len(survival.INTERVALS)
    assert expanded.groupby("_prediction_row").size().eq(len(survival.INTERVALS)).all()
    numeric, categorical = survival.available_features(data)
    assert "invoice_amount_eur" in numeric and "hazard_interval" in categorical
    assert "company" in categorical
    train = survival.expand_hazard_rows(
        data.assign(duration=[3, 19, 46, 75], event=[1, 1, 0, 1]),
        "duration", "event",
    )
    columns = ["invoice_amount_eur", "interval_index", "interval_end_days"]
    categories = ["company", "hazard_interval"]
    prep = survival.make_preprocessor(columns, categories)
    encoded = prep.fit_transform(train)
    assert encoded.shape[0] == len(train)
    model = survival.make_logistic(columns, categories)
    model.fit(train, train.hazard_event)
    assert set(np.unique(model.predict(train))).issubset({0, 1})


def test_recovery_curve_is_monotone_and_probabilities_are_bounded():
    class ConstantHazard:
        def predict_proba(self, frame):
            p = np.full(len(frame), 0.2)
            return np.column_stack([1 - p, p])

    haz, surv, cdf = survival.predict_recovery_curves(ConstantHazard(), cases())
    assert haz.shape == (4, len(survival.INTERVALS))
    assert np.allclose(haz.to_numpy(), 0.2)
    assert np.all(np.diff(surv.to_numpy(), axis=1) <= 0)
    assert np.all(np.diff(cdf.to_numpy(), axis=1) >= 0)
    assert np.all((cdf.to_numpy() >= 0) & (cdf.to_numpy() <= 1))


def test_horizon_scoring_known_rows_and_one_class_behavior():
    cdf = pd.DataFrame({float(h): [0.12, 0.4, 0.65, 0.84]
                        for h in survival.HORIZONS})
    data = cases()
    for h in survival.HORIZONS:
        data[f"clear_within_{h}d"] = [1, 0, None, 1]
    report = survival.horizon_metrics(data, cdf)
    assert set(report) == {str(h) for h in survival.HORIZONS}
    assert all(value["known_rows"] == 3 for value in report.values())
    assert all(0 <= value["brier"] <= 1 for value in report.values())
    assert all(value["roc_auc"] is not None for value in report.values())
    aggregate = survival.aggregate_horizon_metrics(report)
    assert 0 <= aggregate["mean_horizon_brier"] <= 1
    assert 0 <= aggregate["mean_horizon_auc"] <= 1
    data["clear_within_7d"] = [1, 1, None, 1]
    one_class = survival.horizon_metrics(data, cdf)["7"]
    assert one_class["roc_auc"] is None and one_class["pr_auc"] is None
    assert survival.horizon_metrics(data.drop(columns=["clear_within_7d"]), cdf).get("7") is None
    assert survival.expected_calibration_error([0, 1], [0.0, 1.0]) == pytest.approx(0)
    assert survival.expected_calibration_error([0, 1], [0.5, 0.5]) == pytest.approx(0)
