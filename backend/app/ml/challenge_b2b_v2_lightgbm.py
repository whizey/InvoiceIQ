"""
RecoverIQ — LightGBM Challenger Tournament

Compares LightGBM against the already-selected B2B v2 champions
using the SAME:
- temporal folds
- leakage-safe label availability
- feature sets
- stability metrics

TEST SPLIT IS NEVER READ.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from lightgbm import (
    LGBMClassifier,
    LGBMRegressor,
)

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    MODEL_ROOT,
    FEATURE_SETS,
    CLASSIFICATION_TARGETS,
    POSTING_DATE,
    add_targets,
    load_development_data,
    build_temporal_folds,
    evaluate_classifier_candidate,
    aggregate_classifier_folds,
    classifier_selection_key,
    evaluate_regression_candidate,
    aggregate_regression_folds,
    regression_selection_key,
    make_pipeline,
)


OUTPUT_DIR = (
    ARTIFACT_ROOT
    / "lightgbm_challenge"
)

REPORT_PATH = (
    OUTPUT_DIR
    / "challenge_report.json"
)


def banner(title):
    print()
    print("=" * 90)
    print(title)
    print("=" * 90)


def load_metadata(name):
    path = (
        MODEL_ROOT
        / name
        / "metadata.json"
    )

    return json.loads(
        path.read_text()
    )


# ============================================================
# LIGHTGBM FACTORIES
# ============================================================

def make_lgbm_classifier():

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


def make_lgbm_regressor():

    return LGBMRegressor(
        objective="regression_l1",

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


# ============================================================
# QUANTILE TEMPORAL EVALUATION
# ============================================================

def utc(value):

    ts = pd.Timestamp(value)

    if ts.tzinfo is None:
        return ts.tz_localize("UTC")

    return ts.tz_convert("UTC")


def evaluate_quantile(
    dev,
    folds,
    features,
    quantile,
):

    rows = []

    for fold in folds:

        eval_start = utc(
            fold["eval_start"]
        )

        eval_end = (
            utc(
                fold["eval_end"]
            )
            + pd.Timedelta(days=1)
        )

        train = dev[
            (
                dev[POSTING_DATE]
                < eval_start
            )
            &
            (
                dev[
                    "available_days_late"
                ]
                < eval_start
            )
            &
            (
                dev[
                    "target_late_payment"
                ] == 1
            )
        ].copy()

        evaluation = dev[
            (
                dev[POSTING_DATE]
                >= eval_start
            )
            &
            (
                dev[POSTING_DATE]
                < eval_end
            )
            &
            (
                dev[
                    "target_late_payment"
                ] == 1
            )
        ].copy()

        if (
            len(train) < 500
            or len(evaluation) < 50
        ):
            continue

        model = make_pipeline(
            LGBMRegressor(
                objective="quantile",
                alpha=quantile,

                n_estimators=700,
                learning_rate=0.025,

                num_leaves=31,
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
        )

        y_train = train[
            "target_days_late_if_late"
        ].astype(float)

        y_eval = evaluation[
            "target_days_late_if_late"
        ].astype(float)

        model.fit(
            train[features],
            y_train,
        )

        prediction = np.maximum(
            model.predict(
                evaluation[features]
            ),
            0,
        )

        # Pinball loss manually to avoid
        # sklearn-version dependency issues.
        error = (
            y_eval.to_numpy()
            - prediction
        )

        loss = np.mean(
            np.maximum(
                quantile * error,
                (quantile - 1)
                * error,
            )
        )

        coverage = float(
            (
                y_eval.to_numpy()
                <= prediction
            ).mean()
        )

        rows.append(
            {
                "fold":
                    int(
                        fold["fold"]
                    ),

                "rows":
                    int(
                        len(evaluation)
                    ),

                "pinball_loss":
                    float(loss),

                "coverage":
                    coverage,

                "coverage_error":
                    abs(
                        coverage
                        - quantile
                    ),
            }
        )

    return rows


def aggregate_quantile(rows):

    if not rows:
        return {
            "valid": False
        }

    losses = np.array(
        [
            x["pinball_loss"]
            for x in rows
        ]
    )

    coverages = np.array(
        [
            x["coverage"]
            for x in rows
        ]
    )

    errors = np.array(
        [
            x["coverage_error"]
            for x in rows
        ]
    )

    return {
        "valid":
            True,

        "fold_count":
            len(rows),

        "median_pinball":
            float(
                np.median(losses)
            ),

        "worst_pinball":
            float(
                losses.max()
            ),

        "median_coverage":
            float(
                np.median(
                    coverages
                )
            ),

        "median_coverage_error":
            float(
                np.median(
                    errors
                )
            ),
    }


# ============================================================
# MAIN
# ============================================================

def main():

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    banner(
        "RECOVERIQ — LIGHTGBM CHALLENGER TOURNAMENT"
    )

    dev = add_targets(
        load_development_data()
    )

    folds = build_temporal_folds(
        dev
    )

    print(
        f"Development rows: {len(dev):,}"
    )

    print(
        "TEST SPLIT WAS NOT READ."
    )

    report = {
        "classification": {},
        "regression": {},
        "quantiles": {},
        "test_used": False,
    }

    # ========================================================
    # CLASSIFICATION HEADS
    # ========================================================

    for head_name, head in (
        CLASSIFICATION_TARGETS.items()
    ):

        banner(
            f"LIGHTGBM — {head_name.upper()}"
        )

        current = load_metadata(
            head_name
        )

        current_metrics = current[
            "temporal_backtest"
        ]

        print(
            "Current champion:",
            current[
                "model_family"
            ],
            "/",
            current[
                "feature_set"
            ],
        )

        print(
            f"Current median AUC="
            f"{current_metrics['median_roc_auc']:.4f} "
            f"worst="
            f"{current_metrics['worst_roc_auc']:.4f}"
        )

        candidates = []

        for (
            feature_name,
            features,
        ) in FEATURE_SETS.items():

            rows = (
                evaluate_classifier_candidate(
                    dev=dev,
                    folds=folds,
                    target=head[
                        "target"
                    ],
                    availability_column=head[
                        "availability"
                    ],
                    features=features,
                    model_factory=make_lgbm_classifier,
                )
            )

            agg = (
                aggregate_classifier_folds(
                    rows
                )
            )

            if not agg.get(
                "valid"
            ):
                continue

            candidate = {
                "feature_set":
                    feature_name,

                "folds":
                    rows,

                "aggregate":
                    agg,
            }

            candidates.append(
                candidate
            )

            print(
                f"{feature_name:<20}"
                f"median_auc="
                f"{agg['median_roc_auc']:.4f}  "
                f"worst="
                f"{agg['worst_roc_auc']:.4f}  "
                f"std="
                f"{agg['roc_auc_std']:.4f}  "
                f"below50="
                f"{agg['below_random_folds']}  "
                f"PRlift="
                f"{agg['median_pr_lift']:.2f}"
            )

        winner = min(
            candidates,
            key=lambda x:
                classifier_selection_key(
                    x["aggregate"]
                ),
        )

        lgbm_key = (
            classifier_selection_key(
                winner["aggregate"]
            )
        )

        current_key = (
            classifier_selection_key(
                current_metrics
            )
        )

        promote = bool(
            lgbm_key < current_key
        )

        print()
        print(
            "Best LightGBM:",
            winner[
                "feature_set"
            ],
        )

        print(
            f"median={winner['aggregate']['median_roc_auc']:.4f} "
            f"worst={winner['aggregate']['worst_roc_auc']:.4f}"
        )

        print(
            "VERDICT:",
            (
                "PROMOTE LIGHTGBM"
                if promote
                else "KEEP CURRENT CHAMPION"
            )
        )

        report[
            "classification"
        ][
            head_name
        ] = {
            "current_champion":
                current,

            "best_lightgbm":
                winner,

            "promote_lightgbm":
                promote,
        }

    # ========================================================
    # EXPECTED DAYS LATE
    # ========================================================

    banner(
        "LIGHTGBM — CONDITIONAL DAYS LATE"
    )

    current = load_metadata(
        "days_late_if_late_mean"
    )

    current_metrics = current[
        "temporal_backtest"
    ]

    regression_candidates = []

    for (
        feature_name,
        features,
    ) in FEATURE_SETS.items():

        rows = (
            evaluate_regression_candidate(
                dev=dev,
                folds=folds,
                features=features,
                model_factory=make_lgbm_regressor,
            )
        )

        agg = (
            aggregate_regression_folds(
                rows
            )
        )

        if not agg.get(
            "valid"
        ):
            continue

        regression_candidates.append(
            {
                "feature_set":
                    feature_name,

                "folds":
                    rows,

                "aggregate":
                    agg,
            }
        )

        print(
            f"{feature_name:<20}"
            f"median_MAE="
            f"{agg['median_mae']:.3f}  "
            f"worst="
            f"{agg['worst_mae']:.3f}  "
            f"std="
            f"{agg['mae_std']:.3f}"
        )

    reg_winner = min(
        regression_candidates,
        key=lambda x:
            regression_selection_key(
                x["aggregate"]
            ),
    )

    promote_reg = bool(
        regression_selection_key(
            reg_winner["aggregate"]
        )
        <
        regression_selection_key(
            current_metrics
        )
    )

    print()
    print(
        "Current:",
        current[
            "model_family"
        ],
        current[
            "feature_set"
        ],
    )

    print(
        "Best LightGBM:",
        reg_winner[
            "feature_set"
        ],
    )

    print(
        "VERDICT:",
        (
            "PROMOTE LIGHTGBM"
            if promote_reg
            else "KEEP XGBOOST"
        )
    )

    report["regression"] = {
        "current_champion":
            current,

        "best_lightgbm":
            reg_winner,

        "promote_lightgbm":
            promote_reg,
    }

    # ========================================================
    # QUANTILES
    # ========================================================

    for label, q in [
        ("p80", 0.80),
    ]:

        banner(
            f"LIGHTGBM QUANTILE — "
            f"{label.upper()}"
        )

        current = load_metadata(
            f"days_late_{label}"
        )

        current_metrics = current[
            "temporal_backtest"
        ]

        candidates = []

        for (
            feature_name,
            features,
        ) in FEATURE_SETS.items():

            rows = evaluate_quantile(
                dev,
                folds,
                features,
                q,
            )

            agg = aggregate_quantile(
                rows
            )

            if not agg.get(
                "valid"
            ):
                continue

            candidates.append(
                {
                    "feature_set":
                        feature_name,

                    "folds":
                        rows,

                    "aggregate":
                        agg,
                }
            )

            print(
                f"{feature_name:<20}"
                f"pinball="
                f"{agg['median_pinball']:.4f}  "
                f"worst="
                f"{agg['worst_pinball']:.4f}  "
                f"coverage="
                f"{agg['median_coverage']:.3f}  "
                f"error="
                f"{agg['median_coverage_error']:.3f}"
            )

        winner = min(
            candidates,
            key=lambda x: (
                x[
                    "aggregate"
                ][
                    "median_pinball"
                ],

                x[
                    "aggregate"
                ][
                    "median_coverage_error"
                ],

                x[
                    "aggregate"
                ][
                    "worst_pinball"
                ],
            ),
        )

        current_key = (
            current_metrics[
                "median_pinball"
            ],
            current_metrics[
                "median_coverage_error"
            ],
            current_metrics[
                "worst_pinball"
            ],
        )

        lgbm_key = (
            winner[
                "aggregate"
            ][
                "median_pinball"
            ],
            winner[
                "aggregate"
            ][
                "median_coverage_error"
            ],
            winner[
                "aggregate"
            ][
                "worst_pinball"
            ],
        )

        promote = bool(
            lgbm_key
            < current_key
        )

        print()
        print(
            "Current coverage:",
            round(
                current_metrics[
                    "median_coverage"
                ],
                3,
            ),
        )

        print(
            "Best LightGBM:",
            winner[
                "feature_set"
            ],
        )

        print(
            "VERDICT:",
            (
                "PROMOTE LIGHTGBM"
                if promote
                else "KEEP CURRENT QUANTILE MODEL"
            )
        )

        report[
            "quantiles"
        ][
            label
        ] = {
            "current":
                current,

            "best_lightgbm":
                winner,

            "promote_lightgbm":
                promote,
        }

    # ========================================================
    # SAVE
    # ========================================================

    REPORT_PATH.write_text(
        json.dumps(
            report,
            indent=2,
            default=str,
        )
    )

    banner(
        "LIGHTGBM TOURNAMENT COMPLETE"
    )

    print(
        "Report:"
    )

    print(
        " ",
        REPORT_PATH,
    )

    print()
    print(
        "No production champion "
        "was overwritten."
    )

    print(
        "TEST SPLIT WAS NEVER READ."
    )


if __name__ == "__main__":
    main()
