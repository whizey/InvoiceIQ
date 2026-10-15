"""
RecoverIQ — Conditional Late-Days Regression

Business question
-----------------
If an invoice is going to be late, approximately HOW LATE will it be?

This model is intentionally trained only on genuinely late invoices.

System usage
------------
P(late)               -> classification model
late_days_if_late     -> this regression model

expected_days_late =
    P(late) * predicted_late_days_if_late

This is preferable to asking one regression model to model the
large point-mass at zero together with genuinely late invoices.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import TransformedTargetRegressor
from sklearn.ensemble import (
    GradientBoostingRegressor,
    RandomForestRegressor,
)
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    median_absolute_error,
    r2_score,
)
from sklearn.pipeline import Pipeline

from xgboost import XGBRegressor


PROJECT_ROOT = Path(__file__).resolve().parents[3]

SPLIT_DIR = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "primary"
    / "splits"
)

ARTIFACT_ROOT = (
    Path(__file__).resolve().parent
    / "artifacts"
    / "models"
    / "late_days"
)

TARGET = "late_days_if_late"

SOURCE_TARGET = "days_late_weekend_adjusted"
LATE_FLAG = "late_payment_weekend_adjusted"


FEATURES = [
    "amount",
    "payment_term",
    "prior_resolved_invoice_count",
    "prior_open_invoice_count",
    "open_invoice_ratio",
    "has_resolved_history",
    "historical_late_rate",
    "historical_avg_days_late",
    "historical_avg_invoice_amount",
    "amount_vs_historical_avg_ratio",
    "customer_tenure_days",
    "days_since_previous_invoice",
    "recent_5_late_rate",
    "recent_5_avg_days_late",
    "recent_late_streak",
    "recent_on_time_streak",
    "recent_5_late_rate_delta",
    "recent_5_avg_days_late_delta",
]


def banner(text: str) -> None:
    print()
    print("=" * 76)
    print(text)
    print("=" * 76)


def load_split(name: str) -> pd.DataFrame:
    path = SPLIT_DIR / f"{name}.csv.gz"

    if not path.exists():
        raise FileNotFoundError(path)

    return pd.read_csv(path)


def prepare(frame: pd.DataFrame) -> pd.DataFrame:

    required = (
        set(FEATURES)
        | {
            SOURCE_TARGET,
            LATE_FLAG,
        }
    )

    missing = required - set(frame.columns)

    if missing:
        raise ValueError(
            f"Missing columns: {sorted(missing)}"
        )

    x = frame.copy()

    # Explicit business target.
    x[TARGET] = (
        pd.to_numeric(
            x[SOURCE_TARGET],
            errors="coerce",
        )
        .clip(lower=0)
    )

    # Conditional model:
    # only invoices that were actually late.
    x = x[
        x[LATE_FLAG] == 1
    ].copy()

    x = x[
        x[TARGET].notna()
        & (x[TARGET] > 0)
    ].copy()

    return x


def metrics(y_true, y_pred) -> dict:

    y_pred = np.maximum(
        np.asarray(y_pred),
        0,
    )

    return {
        "mae": float(
            mean_absolute_error(
                y_true,
                y_pred,
            )
        ),

        "median_absolute_error": float(
            median_absolute_error(
                y_true,
                y_pred,
            )
        ),

        "rmse": float(
            np.sqrt(
                mean_squared_error(
                    y_true,
                    y_pred,
                )
            )
        ),

        "r2": float(
            r2_score(
                y_true,
                y_pred,
            )
        ),
    }


def make_pipeline(model):

    return Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(
                    strategy="median"
                ),
            ),
            (
                "model",
                model,
            ),
        ]
    )


def main():

    banner(
        "RECOVERIQ — LATE-DAYS REGRESSION"
    )

    train_raw = load_split("train")
    validation_raw = load_split(
        "validation"
    )

    train = prepare(train_raw)
    validation = prepare(
        validation_raw
    )

    print(
        f"Train late invoices:      "
        f"{len(train):,}"
    )

    print(
        f"Validation late invoices: "
        f"{len(validation):,}"
    )

    print()
    print(
        "Train target distribution:"
    )

    print(
        train[TARGET]
        .describe(
            percentiles=[
                .25,
                .50,
                .75,
                .90,
                .95,
                .99,
            ]
        )
        .round(2)
        .to_string()
    )

    X_train = train[FEATURES]
    y_train = train[TARGET]

    X_val = validation[FEATURES]
    y_val = validation[TARGET]

    # Log1p transformation makes the model less dominated
    # by a small number of extremely late invoices.
    models = {
        "random_forest": (
            RandomForestRegressor(
                n_estimators=600,
                max_depth=12,
                min_samples_leaf=30,
                max_features=0.5,
                n_jobs=-1,
                random_state=42,
            )
        ),

        "xgboost": (
            XGBRegressor(
                n_estimators=700,
                learning_rate=0.03,
                max_depth=4,
                min_child_weight=20,
                subsample=0.85,
                colsample_bytree=0.8,
                objective="reg:squarederror",
                eval_metric="mae",
                random_state=42,
                n_jobs=-1,
            )
        ),

        "gradient_boosting": (
            GradientBoostingRegressor(
                n_estimators=500,
                learning_rate=0.03,
                max_depth=3,
                min_samples_leaf=20,
                subsample=0.85,
                loss="huber",
                random_state=42,
            )
        ),
    }

    results = {}

    for name, base_model in models.items():

        banner(
            f"TRAINING — {name.upper()}"
        )

        pipeline = make_pipeline(
            base_model
        )

        # days late is normally right-skewed.
        # Fit in log-space but return predictions
        # in original day units.
        model = TransformedTargetRegressor(
            regressor=pipeline,
            func=np.log1p,
            inverse_func=np.expm1,
        )

        model.fit(
            X_train,
            y_train,
        )

        pred_train = model.predict(
            X_train
        )

        pred_val = model.predict(
            X_val
        )

        train_metrics = metrics(
            y_train,
            pred_train,
        )

        val_metrics = metrics(
            y_val,
            pred_val,
        )

        print("\nTRAIN")
        for k, v in train_metrics.items():
            print(
                f"{k:<25} {v:.4f}"
            )

        print("\nVALIDATION")
        for k, v in val_metrics.items():
            print(
                f"{k:<25} {v:.4f}"
            )

        model_dir = (
            ARTIFACT_ROOT / name
        )

        model_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        joblib.dump(
            model,
            model_dir / "model.joblib",
        )

        report = {
            "task": (
                "conditional_late_days_regression"
            ),

            "business_question": (
                "If the invoice is late, "
                "approximately how many days "
                "late will it be?"
            ),

            "target": TARGET,

            "source_target":
                SOURCE_TARGET,

            "training_population": (
                "Invoices where "
                "late_payment_weekend_adjusted == 1"
            ),

            "features": FEATURES,

            "train_rows": int(
                len(train)
            ),

            "validation_rows": int(
                len(validation)
            ),

            "train_metrics":
                train_metrics,

            "validation_metrics":
                val_metrics,

            "test_used": False,

            "notes": [
                (
                    "Model selection uses validation only."
                ),
                (
                    "Test split remains untouched."
                ),
                (
                    "Target is log1p transformed during "
                    "training because lateness duration "
                    "is typically right-skewed."
                ),
            ],
        }

        (
            model_dir / "report.json"
        ).write_text(
            json.dumps(
                report,
                indent=2,
            )
        )

        results[name] = {
            "model": model,
            "validation":
                val_metrics,
        }

    # Lower validation MAE wins.
    winner = min(
        results,
        key=lambda name: (
            results[name][
                "validation"
            ]["mae"]
        ),
    )

    banner(
        "MODEL SELECTION"
    )

    for name, result in results.items():

        val = result[
            "validation"
        ]

        print(
            f"{name:<22}"
            f"MAE={val['mae']:.3f}  "
            f"MedAE="
            f"{val['median_absolute_error']:.3f}  "
            f"RMSE={val['rmse']:.3f}"
        )

    print()
    print(
        "Selected:",
        winner,
    )

    selection = {
        "selected_model": winner,

        "selection_metric":
            "validation_mae",

        "test_used": False,

        "models": {
            name: value[
                "validation"
            ]
            for name, value
            in results.items()
        },
    }

    ARTIFACT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    (
        ARTIFACT_ROOT
        / "selection.json"
    ).write_text(
        json.dumps(
            selection,
            indent=2,
        )
    )

    banner(
        "LATE-DAYS TRAINING COMPLETE"
    )


if __name__ == "__main__":
    main()
