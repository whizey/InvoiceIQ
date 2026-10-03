from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
import shap
import xgboost as xgb
from sklearn.ensemble import (
    GradientBoostingClassifier,
    RandomForestClassifier,
)

import app.ml.package_payment_risk_artifacts as packager
import app.ml.payment_risk_model as runtime
from app.ml.artifact_contract import (
    save_training_contract,
    sha256_file,
)
from app.ml.payment_features import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
    PaymentRiskFeatures,
)


MODEL_NAMES = [
    "xgboost",
    "random_forest",
    "gradient_boosting",
]


def _make_toy_data(
    seed: int = 20261003,
) -> tuple[
    pd.DataFrame,
    np.ndarray,
]:
    rng = np.random.default_rng(seed)

    rows = 180

    X = pd.DataFrame(
        rng.normal(
            size=(
                rows,
                len(FEATURES),
            )
        ),
        columns=FEATURES,
    )

    # Give several features more realistic ranges.
    X["amount"] = rng.uniform(
        100.0,
        500_000.0,
        rows,
    )

    X["payment_term"] = rng.choice(
        [7, 15, 30, 45, 60, 90],
        size=rows,
    )

    X[
        "prior_resolved_invoice_count"
    ] = rng.integers(
        0,
        100,
        rows,
    )

    X[
        "prior_open_invoice_count"
    ] = rng.integers(
        0,
        30,
        rows,
    )

    X["open_invoice_ratio"] = rng.uniform(
        0.0,
        1.0,
        rows,
    )

    X["has_resolved_history"] = (
        X[
            "prior_resolved_invoice_count"
        ]
        > 0
    ).astype(float)

    X["historical_late_rate"] = (
        rng.uniform(
            0.0,
            1.0,
            rows,
        )
    )

    X["recent_5_late_rate"] = (
        rng.uniform(
            0.0,
            1.0,
            rows,
        )
    )

    X[
        "historical_avg_days_late"
    ] = rng.normal(
        5.0,
        20.0,
        rows,
    )

    X[
        "recent_5_avg_days_late"
    ] = rng.normal(
        5.0,
        20.0,
        rows,
    )

    X[
        "historical_avg_invoice_amount"
    ] = rng.uniform(
        100.0,
        500_000.0,
        rows,
    )

    X[
        "amount_vs_historical_avg_ratio"
    ] = rng.uniform(
        0.0,
        20.0,
        rows,
    )

    X["customer_tenure_days"] = (
        rng.integers(
            0,
            501,
            rows,
        )
    )

    X[
        "days_since_previous_invoice"
    ] = rng.integers(
        0,
        501,
        rows,
    )

    X["recent_late_streak"] = (
        rng.integers(
            0,
            10,
            rows,
        )
    )

    X["recent_on_time_streak"] = (
        rng.integers(
            0,
            10,
            rows,
        )
    )

    X[
        "recent_5_late_rate_delta"
    ] = (
        X["recent_5_late_rate"]
        - X["historical_late_rate"]
    )

    X[
        "recent_5_avg_days_late_delta"
    ] = (
        X["recent_5_avg_days_late"]
        - X[
            "historical_avg_days_late"
        ]
    )

    # Exercise the real cold-start NaN path.
    cold_indices = [
        0,
        7,
        21,
        52,
    ]

    X.loc[
        cold_indices,
        "has_resolved_history",
    ] = 0.0

    X.loc[
        cold_indices,
        "historical_late_rate",
    ] = np.nan

    X.loc[
        cold_indices,
        "recent_5_late_rate",
    ] = np.nan

    # Deterministic target with nonlinear signal.
    signal = (
        1.8
        * X[
            "prior_open_invoice_count"
        ]
        / 30.0
        + 1.2
        * X[
            "open_invoice_ratio"
        ]
        + 0.8
        * X[
            "customer_tenure_days"
        ]
        / 500.0
        + 0.7
        * X[
            "payment_term"
        ]
        / 90.0
        + rng.normal(
            0.0,
            0.35,
            rows,
        )
    )

    threshold = float(
        np.nanmedian(signal)
    )

    y = (
        signal > threshold
    ).astype(int).to_numpy()

    assert set(y) == {0, 1}
    assert list(X.columns) == FEATURES

    return X, y


def _training_medians(
    X: pd.DataFrame,
) -> dict[str, float]:
    medians = {}

    for column in NUMERIC_FEATURES:
        medians[column] = float(
            pd.to_numeric(
                X[column],
                errors="coerce",
            ).median()
        )

    return medians


def _make_features(
    X: pd.DataFrame,
) -> PaymentRiskFeatures:
    # Use a cold-start row so prediction also checks
    # that RF/GB runtime median imputation works.
    row = X.iloc[0]

    values = {}

    for feature in FEATURES:
        value = row[feature]

        if pd.isna(value):
            values[feature] = float("nan")
        else:
            values[feature] = float(value)

    return PaymentRiskFeatures(
        **values
    )


def _positive_expected_value(
    explainer,
) -> float:
    values = np.asarray(
        explainer.expected_value
    )

    if values.ndim == 0:
        return float(values)

    flat = values.reshape(-1)

    if flat.size >= 2:
        return float(flat[1])

    return float(flat[0])


@pytest.fixture()
def toy_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    assert len(FEATURES) == 18
    assert len(NUMERIC_FEATURES) == 18
    assert CATEGORICAL_FEATURES == []

    artifact_root = (
        tmp_path / "models"
    )

    artifact_root.mkdir(
        parents=True
    )

    X, y = _make_toy_data()

    train_split = (
        tmp_path / "train.csv.gz"
    )

    split_frame = X.copy()

    split_frame[TARGET_COLUMN] = y

    split_frame.to_csv(
        train_split,
        index=False,
        compression="gzip",
    )

    medians = _training_medians(X)

    X_imputed = X.copy()

    for column in NUMERIC_FEATURES:
        X_imputed[column] = (
            pd.to_numeric(
                X_imputed[column],
                errors="coerce",
            )
            .fillna(
                medians[column]
            )
        )

    # --------------------------------------------------
    # XGBoost
    # --------------------------------------------------

    xgb_dir = (
        artifact_root / "xgboost"
    )

    xgb_dir.mkdir()

    xgb_model = xgb.XGBClassifier(
        n_estimators=20,
        max_depth=2,
        learning_rate=0.1,
        min_child_weight=1,
        reg_lambda=1.0,
        eval_metric="logloss",
        random_state=42,
        tree_method="hist",
        n_jobs=1,
    )

    xgb_model.fit(
        X,
        y,
    )

    xgb_path = (
        xgb_dir / "model.json"
    )

    xgb_model.save_model(
        str(xgb_path)
    )

    save_training_contract(
        model_name="xgboost",
        directory=xgb_dir,
        model_path=xgb_path,
        train_split_path=(
            train_split
        ),
        category_levels={},
    )

    # --------------------------------------------------
    # Random Forest
    # --------------------------------------------------

    rf_dir = (
        artifact_root
        / "random_forest"
    )

    rf_dir.mkdir()

    rf_model = (
        RandomForestClassifier(
            n_estimators=25,
            max_depth=4,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=1,
        )
    )

    rf_model.fit(
        X_imputed,
        y,
    )

    rf_path = (
        rf_dir / "model.joblib"
    )

    joblib.dump(
        rf_model,
        rf_path,
    )

    rf_median_path = (
        rf_dir
        / "median_impute_values.json"
    )

    rf_median_path.write_text(
        json.dumps(
            medians,
            indent=2,
        )
        + "\n"
    )

    save_training_contract(
        model_name="random_forest",
        directory=rf_dir,
        model_path=rf_path,
        train_split_path=(
            train_split
        ),
        category_levels={},
        median_path=(
            rf_median_path
        ),
    )

    # --------------------------------------------------
    # Gradient Boosting
    # --------------------------------------------------

    gb_dir = (
        artifact_root
        / "gradient_boosting"
    )

    gb_dir.mkdir()

    gb_model = (
        GradientBoostingClassifier(
            n_estimators=25,
            learning_rate=0.05,
            max_depth=2,
            random_state=42,
        )
    )

    gb_model.fit(
        X_imputed,
        y,
    )

    gb_path = (
        gb_dir / "model.joblib"
    )

    joblib.dump(
        gb_model,
        gb_path,
    )

    gb_median_path = (
        gb_dir
        / "median_impute_values.json"
    )

    gb_median_path.write_text(
        json.dumps(
            medians,
            indent=2,
        )
        + "\n"
    )

    save_training_contract(
        model_name="gradient_boosting",
        directory=gb_dir,
        model_path=gb_path,
        train_split_path=(
            train_split
        ),
        category_levels={},
        median_path=(
            gb_median_path
        ),
    )

    # --------------------------------------------------
    # Run the REAL packager against only tmp_path.
    # --------------------------------------------------

    monkeypatch.setattr(
        packager,
        "ARTIFACT_ROOT",
        artifact_root,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "package_payment_risk_artifacts",
            "--confirm-retrained",
        ],
    )

    packager.main()

    # Point the REAL runtime loader at tmp_path.
    monkeypatch.setattr(
        runtime,
        "ARTIFACT_ROOT",
        artifact_root,
    )

    runtime._load_contract.cache_clear()
    runtime._load_model.cache_clear()
    runtime._load_shap_explainer.cache_clear()

    yield {
        "root": artifact_root,
        "X": X,
        "y": y,
        "features": _make_features(X),
    }

    runtime._load_contract.cache_clear()
    runtime._load_model.cache_clear()


def test_real_packager_writes_runtime_contracts(
    toy_runtime,
):
    root = toy_runtime["root"]

    for model_name in MODEL_NAMES:
        directory = (
            root / model_name
        )

        path = (
            directory
            / "runtime_contract.json"
        )

        assert path.exists()

        contract = json.loads(
            path.read_text()
        )

        assert (
            contract["schema_version"]
            == 2
        )

        assert (
            contract["model"]
            == model_name
        )

        assert (
            contract["target"]
            == TARGET_COLUMN
        )

        assert (
            contract["features"]
            == FEATURES
        )

        assert (
            contract["numeric"]
            == NUMERIC_FEATURES
        )

        assert (
            contract["categorical"]
            == []
        )

        assert (
            contract["category_levels"]
            == {}
        )

        assert contract[
            "model_version"
        ].startswith(
            f"{model_name}:"
        )

        model_file = (
            directory
            / contract["model_file"]
        )

        assert (
            sha256_file(model_file)
            == contract[
                "model_sha256"
            ]
        )

        if (
            model_name
            != "xgboost"
        ):
            median_file = (
                directory
                / contract[
                    "median_file"
                ]
            )

            assert (
                sha256_file(
                    median_file
                )
                == contract[
                    "median_sha256"
                ]
            )


def test_toy_models_load_and_predict(
    toy_runtime,
):
    features = (
        toy_runtime["features"]
    )

    for model_name in MODEL_NAMES:
        prediction = (
            runtime.predict_payment_risk(
                features,
                model_name,
            )
        )

        assert (
            prediction.model_name
            == model_name
        )

        assert (
            0.0
            <= prediction.late_payment_probability
            <= 1.0
        )

        assert (
            len(
                prediction.contributions
            )
            == len(FEATURES)
        )

        assert {
            contribution.feature
            for contribution
            in prediction.contributions
        } == set(FEATURES)

        for contribution in (
            prediction.contributions
        ):
            assert math.isfinite(
                contribution.shap_value
            )

        contribution_map = {
            item.feature: item
            for item
            in prediction.contributions
        }

        # The selected fixture is cold-start.
        assert (
            contribution_map[
                "historical_late_rate"
            ].value
            is None
        )

        assert (
            contribution_map[
                "recent_5_late_rate"
            ].value
            is None
        )


def test_shap_additivity_all_three_models(
    toy_runtime,
):
    features = (
        toy_runtime["features"]
    )

    for model_name in MODEL_NAMES:
        _, prepared = (
            runtime._prepare_frames(
                model_name,
                features,
            )
        )

        model = runtime._load_model(
            model_name
        )

        prediction = (
            runtime.predict_payment_risk(
                features,
                model_name,
            )
        )

        contribution_sum = sum(
            item.shap_value
            for item
            in prediction.contributions
        )

        if model_name == "xgboost":
            matrix = xgb.DMatrix(
                prepared,
                enable_categorical=True,
            )

            raw_contributions = (
                model
                .get_booster()
                .predict(
                    matrix,
                    pred_contribs=True,
                )[0]
            )

            base_value = float(
                raw_contributions[-1]
            )

            raw_margin = float(
                model
                .get_booster()
                .predict(
                    matrix,
                    output_margin=True,
                )[0]
            )

            assert (
                base_value
                + float(
                    np.sum(
                        raw_contributions[
                            :-1
                        ]
                    )
                )
                == pytest.approx(
                    raw_margin,
                    abs=1e-5,
                )
            )

            # Runtime values are rounded to 6 decimal
            # places, so use a slightly wider tolerance.
            assert (
                base_value
                + contribution_sum
                == pytest.approx(
                    raw_margin,
                    abs=2e-5,
                )
            )

            probability = (
                1.0
                / (
                    1.0
                    + math.exp(
                        -raw_margin
                    )
                )
            )

            assert probability == pytest.approx(
                model.predict_proba(
                    prepared
                )[0][1],
                abs=1e-6,
            )

            continue

        explainer = (
            shap.TreeExplainer(
                model
            )
        )

        base_value = (
            _positive_expected_value(
                explainer
            )
        )

        shap_values = (
            runtime._positive_class_shap(
                model_name,
                prepared,
            )
        )

        assert (
            len(shap_values)
            == len(FEATURES)
        )

        reconstructed = (
            base_value
            + float(
                np.sum(
                    shap_values
                )
            )
        )

        if (
            model_name
            == "gradient_boosting"
        ):
            expected = float(
                model.decision_function(
                    prepared
                )[0]
            )

            assert reconstructed == pytest.approx(
                expected,
                abs=1e-5,
            )

            # Runtime contributions are rounded.
            assert (
                base_value
                + contribution_sum
                == pytest.approx(
                    expected,
                    abs=2e-5,
                )
            )

        elif (
            model_name
            == "random_forest"
        ):
            expected = float(
                model.predict_proba(
                    prepared
                )[0][1]
            )

            assert reconstructed == pytest.approx(
                expected,
                abs=1e-5,
            )

            assert (
                base_value
                + contribution_sum
                == pytest.approx(
                    expected,
                    abs=2e-5,
                )
            )


def test_runtime_rejects_missing_median_file(
    toy_runtime,
):
    root = toy_runtime["root"]

    median_path = (
        root
        / "random_forest"
        / "median_impute_values.json"
    )

    median_path.unlink()

    runtime._load_contract.cache_clear()
    runtime._load_model.cache_clear()

    with pytest.raises(
        runtime.PaymentRiskArtifactError,
        match="Missing",
    ):
        runtime.predict_payment_risk(
            toy_runtime["features"],
            "random_forest",
        )


def test_runtime_rejects_feature_contract_mismatch(
    toy_runtime,
):
    root = toy_runtime["root"]

    contract_path = (
        root
        / "xgboost"
        / "runtime_contract.json"
    )

    contract = json.loads(
        contract_path.read_text()
    )

    contract["features"] = list(
        reversed(
            contract["features"]
        )
    )

    contract_path.write_text(
        json.dumps(
            contract,
            indent=2,
        )
        + "\n"
    )

    runtime._load_contract.cache_clear()
    runtime._load_model.cache_clear()
    runtime._load_shap_explainer.cache_clear()

    with pytest.raises(
        runtime.PaymentRiskArtifactError,
        match="feature order",
    ):
        runtime.predict_payment_risk(
            toy_runtime["features"],
            "xgboost",
        )


def test_shap_explainer_is_cached(
    toy_runtime,
):
    first = (
        runtime._load_shap_explainer(
            "random_forest"
        )
    )

    second = (
        runtime._load_shap_explainer(
            "random_forest"
        )
    )

    assert first is second


def test_runtime_rejects_model_hash_mismatch(
    toy_runtime,
):
    root = toy_runtime["root"]

    model_path = (
        root
        / "xgboost"
        / "model.json"
    )

    model_path.write_bytes(
        model_path.read_bytes()
        + b"\n"
    )

    runtime._load_contract.cache_clear()
    runtime._load_model.cache_clear()
    runtime._load_shap_explainer.cache_clear()

    with pytest.raises(
        runtime.PaymentRiskArtifactError,
        match="model artifact hash mismatch",
    ):
        runtime.predict_payment_risk(
            toy_runtime["features"],
            "xgboost",
        )


def test_runtime_rejects_median_hash_mismatch(
    toy_runtime,
):
    root = toy_runtime["root"]

    median_path = (
        root
        / "gradient_boosting"
        / "median_impute_values.json"
    )

    medians = json.loads(
        median_path.read_text()
    )

    first_feature = (
        NUMERIC_FEATURES[0]
    )

    medians[first_feature] = (
        float(
            medians[first_feature]
        )
        + 1.0
    )

    median_path.write_text(
        json.dumps(
            medians,
            indent=2,
        )
        + "\n"
    )

    runtime._load_contract.cache_clear()
    runtime._load_model.cache_clear()
    runtime._load_shap_explainer.cache_clear()

    with pytest.raises(
        runtime.PaymentRiskArtifactError,
        match="median artifact hash mismatch",
    ):
        runtime.predict_payment_risk(
            toy_runtime["features"],
            "gradient_boosting",
        )
