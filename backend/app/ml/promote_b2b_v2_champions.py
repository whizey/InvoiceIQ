"""
RecoverIQ — Freeze final B2B v2 development champions.

Promotes:
    late_payment -> LightGBM / behaviour_heavy

Keeps:
    delay_over_7d -> Random Forest + Platt
    delay_over_15d -> Random Forest experimental
    days late -> XGBoost
    conservative clearance window -> existing P80 GB quantile model

RESERVED TEST SPLIT IS NEVER READ.
"""

from __future__ import annotations

import json
import shutil

import joblib
import pandas as pd

from lightgbm import LGBMClassifier

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    MODEL_ROOT,
    FEATURE_SETS,
    add_targets,
    load_development_data,
    make_pipeline,
)


def make_lgbm():
    return LGBMClassifier(
        objective="binary",
        n_estimators=700,
        learning_rate=0.025,
        num_leaves=31,
        max_depth=-1,
        min_child_samples=40,
        subsample=0.85,
        subsample_freq=1,
        colsample_bytree=0.80,
        reg_alpha=1.0,
        reg_lambda=10.0,
        random_state=42,
        n_jobs=-1,
        verbosity=-1,
    )


def main():

    print("=" * 84)
    print("RECOVERIQ — PROMOTE B2B V2 CHAMPIONS")
    print("=" * 84)

    dev = add_targets(
        load_development_data()
    )

    # Deployment immediately after development window.
    refit_as_of = (
        dev["posting_date"]
        .max()
        .normalize()
        + pd.Timedelta(days=1)
    )

    # --------------------------------------------------------
    # Late-payment champion
    # --------------------------------------------------------

    features = FEATURE_SETS[
        "behaviour_heavy"
    ]

    train = dev[
        dev[
            "available_late_payment"
        ]
        < refit_as_of
    ].copy()

    model = make_pipeline(
        make_lgbm()
    )

    model.fit(
        train[features],
        train[
            "target_late_payment"
        ].astype(int),
    )

    model_dir = (
        MODEL_ROOT
        / "late_payment"
    )

    old_model = (
        model_dir
        / "model.joblib"
    )

    old_metadata = (
        model_dir
        / "metadata.json"
    )

    # Preserve previous GB champion.
    if old_model.exists():
        shutil.copy2(
            old_model,
            model_dir
            / "model_pre_lightgbm.joblib",
        )

    if old_metadata.exists():
        shutil.copy2(
            old_metadata,
            model_dir
            / "metadata_pre_lightgbm.json",
        )

    joblib.dump(
        model,
        old_model,
    )

    challenge_report = json.loads(
        (
            ARTIFACT_ROOT
            / "lightgbm_challenge"
            / "challenge_report.json"
        ).read_text()
    )

    temporal = (
        challenge_report[
            "classification"
        ][
            "late_payment"
        ][
            "best_lightgbm"
        ][
            "aggregate"
        ]
    )

    metadata = {
        "task":
            "classification",

        "head":
            "late_payment",

        "business_question":
            "Will this invoice miss its adjusted due date?",

        "model_family":
            "lightgbm",

        "feature_set":
            "behaviour_heavy",

        "features":
            features,

        "training_rows":
            int(len(train)),

        "positive_rate":
            float(
                train[
                    "target_late_payment"
                ].mean()
            ),

        "refit_as_of":
            str(refit_as_of),

        "temporal_backtest":
            temporal,

        "serving_calibration":
            "raw",

        "status":
            "SUPPORTED_UNCALIBRATED",

        "test_used":
            False,
    }

    old_metadata.write_text(
        json.dumps(
            metadata,
            indent=2,
        )
    )

    # --------------------------------------------------------
    # Freeze full champion manifest
    # --------------------------------------------------------

    champions = {
        "late_payment": {
            "model":
                "LightGBM",

            "feature_set":
                "behaviour_heavy",

            "calibration":
                "raw",

            "status":
                "SUPPORTED_UNCALIBRATED",
        },

        "delay_over_7d": {
            "model":
                "RandomForest",

            "feature_set":
                "no_open_ratio",

            "calibration":
                "Platt",

            "status":
                "SUPPORTED_CALIBRATED",
        },

        "delay_over_15d": {
            "model":
                "RandomForest",

            "feature_set":
                "behaviour_heavy",

            "calibration":
                "none",

            "status":
                "EXPERIMENTAL_UNCALIBRATED",
        },

        "days_late_if_late_mean": {
            "model":
                "XGBoost",

            "feature_set":
                "no_open_ratio",

            "status":
                "SUPPORTED",
        },

        "days_late_p80": {
            "model":
                "GradientBoostingQuantile",

            "feature_set":
                "behaviour_heavy",

            "status":
                "SUPPORTED_PROVISIONAL",
        },

        "test_split_read":
            False,
    }

    manifest = (
        ARTIFACT_ROOT
        / "champion_manifest.json"
    )

    manifest.write_text(
        json.dumps(
            champions,
            indent=2,
        )
    )

    print()
    print(
        "PROMOTED:"
    )

    print(
        " late_payment -> "
        "LightGBM / behaviour_heavy"
    )

    print()
    print(
        "KEPT:"
    )

    print(
        " >7d        -> RF + Platt"
    )
    print(
        " >15d       -> RF experimental"
    )
    print(
        " late days  -> XGBoost"
    )
    print(
        " clearance window (P80) -> GB quantile provisional"
    )

    print()
    print(
        "Champion manifest:"
    )
    print(
        " ",
        manifest,
    )

    print()
    print(
        "TEST SPLIT WAS NEVER READ."
    )


if __name__ == "__main__":
    main()
