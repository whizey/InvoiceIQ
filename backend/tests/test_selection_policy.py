from __future__ import annotations

import copy

import pytest

from app.ml.payment_features import (
    FEATURES,
    TARGET_COLUMN,
)
from app.ml.selection_policy import (
    ModelSelectionError,
    PRIMARY_SELECTION_METRIC,
    select_final_model,
)


def _report(
    model: str,
    within: float,
    pooled: float,
) -> dict:
    return {
        "model": model,
        "target": TARGET_COLUMN,
        "test_used": False,
        "features": {
            "order": FEATURES,
        },
        "validation": {
            "within_period_auc": (
                within
            ),
            "pooled_roc_auc": (
                pooled
            ),
            "pooled_pr_auc": 0.50,
            "log_loss": 0.60,
            "brier": 0.20,
        },
        "temporal_folds": {
            "mean": 0.60,
            "min": 0.40,
            "max": 0.80,
            "std": 0.15,
            "inverted_folds": [0],
            "stable": False,
        },
    }


def _reports() -> dict:
    return {
        "xgboost": _report(
            "xgboost",
            within=0.81,
            pooled=0.99,
        ),
        "random_forest": _report(
            "random_forest",
            within=0.84,
            pooled=0.70,
        ),
        "gradient_boosting": (
            _report(
                "gradient_boosting",
                within=0.79,
                pooled=0.98,
            )
        ),
    }


def test_primary_metric_is_frozen():
    assert (
        PRIMARY_SELECTION_METRIC
        == "within_period_auc"
    )


def test_selection_uses_validation_within_period_auc():
    result = select_final_model(
        _reports()
    )

    assert (
        result.model_name
        == "random_forest"
    )

    assert (
        result.primary_value
        == pytest.approx(0.84)
    )


def test_pooled_auc_cannot_override_frozen_rule():
    reports = _reports()

    reports["xgboost"][
        "validation"
    ]["pooled_roc_auc"] = 1.0

    result = select_final_model(
        reports
    )

    assert (
        result.model_name
        == "random_forest"
    )


def test_selection_refuses_missing_model_family():
    reports = _reports()

    del reports[
        "gradient_boosting"
    ]

    with pytest.raises(
        ModelSelectionError,
        match="Missing final model reports",
    ):
        select_final_model(
            reports
        )


def test_selection_refuses_test_contamination():
    reports = _reports()

    reports["xgboost"][
        "test_used"
    ] = True

    with pytest.raises(
        ModelSelectionError,
        match="reserved test split",
    ):
        select_final_model(
            reports
        )


def test_selection_refuses_feature_contract_drift():
    reports = copy.deepcopy(
        _reports()
    )

    reports[
        "random_forest"
    ]["features"]["order"] = (
        list(reversed(FEATURES))
    )

    with pytest.raises(
        ModelSelectionError,
        match="feature contract",
    ):
        select_final_model(
            reports
        )
