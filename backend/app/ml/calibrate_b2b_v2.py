"""
RecoverIQ — B2B v2 Calibration & Business Evaluation
=====================================================

Does NOT read the reserved test split.

Adds:
- temporal out-of-fold probability calibration
- quantile calibration
- cross-head monotonic consistency
- business ranking metrics
- agent answer contract
"""

from __future__ import annotations

import json

import joblib
import numpy as np
import pandas as pd

from sklearn.ensemble import GradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import mean_pinball_loss

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    MODEL_ROOT,
    CLASSIFICATION_TARGETS,
    POSTING_DATE,
    load_development_data,
    add_targets,
    build_temporal_folds,
    make_pipeline,
    classifier_factories,
    classifier_metrics,
)


CALIBRATION_DIR = (
    ARTIFACT_ROOT
    / "calibration"
)

OOF_PATH = (
    CALIBRATION_DIR
    / "oof_predictions.csv.gz"
)

BUSINESS_METRICS_PATH = (
    CALIBRATION_DIR
    / "business_metrics.json"
)

QUANTILE_PATH = (
    CALIBRATION_DIR
    / "quantile_calibration.json"
)

AGENT_CONTRACT_PATH = (
    ARTIFACT_ROOT
    / "agent_answer_contract.json"
)


def banner(title: str) -> None:
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)

    if ts.tzinfo is None:
        return ts.tz_localize("UTC")

    return ts.tz_convert("UTC")


def load_metadata(name: str) -> dict:
    path = MODEL_ROOT / name / "metadata.json"

    if not path.exists():
        raise FileNotFoundError(path)

    return json.loads(
        path.read_text()
    )


# ============================================================
# CALIBRATORS
# ============================================================

def logit_probability(p):
    p = np.clip(
        np.asarray(p),
        1e-6,
        1 - 1e-6,
    )

    return np.log(
        p / (1 - p)
    )


def fit_probability_calibrator(
    probability,
    y,
):
    probability = np.asarray(
        probability,
        dtype=float,
    )

    y = np.asarray(
        y,
        dtype=int,
    )

    positives = int(y.sum())
    negatives = int(
        len(y) - positives
    )

    unique_predictions = len(
        np.unique(
            np.round(
                probability,
                6,
            )
        )
    )

    # Isotonic when support is sufficient.
    if (
        positives >= 100
        and negatives >= 100
        and unique_predictions >= 20
    ):
        model = IsotonicRegression(
            y_min=0.0,
            y_max=1.0,
            out_of_bounds="clip",
        )

        model.fit(
            probability,
            y,
        )

        return {
            "kind": "isotonic",
            "model": model,
        }

    # Platt-style fallback.
    model = LogisticRegression(
        random_state=42,
        max_iter=2000,
    )

    model.fit(
        logit_probability(
            probability
        ).reshape(-1, 1),
        y,
    )

    return {
        "kind": "platt",
        "model": model,
    }


def apply_calibrator(
    calibrator,
    probability,
):
    probability = np.asarray(
        probability,
        dtype=float,
    )

    if calibrator["kind"] == "isotonic":
        output = calibrator[
            "model"
        ].predict(
            probability
        )

    else:
        output = calibrator[
            "model"
        ].predict_proba(
            logit_probability(
                probability
            ).reshape(-1, 1)
        )[:, 1]

    return np.clip(
        output,
        0,
        1,
    )


# ============================================================
# CLASSIFIER OOF
# ============================================================

def classifier_oof(
    dev,
    folds,
    head_name,
):
    metadata = load_metadata(
        head_name
    )

    head = CLASSIFICATION_TARGETS[
        head_name
    ]

    features = metadata[
        "features"
    ]

    model_family = metadata[
        "model_family"
    ]

    factory = classifier_factories()[
        model_family
    ]

    output = []

    for fold in folds:
        eval_start = utc(
            fold["eval_start"]
        )

        eval_end = utc(
            fold["eval_end"]
        ) + pd.Timedelta(days=1)

        train = dev[
            (
                dev[POSTING_DATE]
                < eval_start
            )
            &
            (
                dev[
                    head["availability"]
                ]
                < eval_start
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
        ].copy()

        if (
            len(train) < 1000
            or len(evaluation) < 100
        ):
            continue

        y_train = train[
            head["target"]
        ].astype(int)

        y_eval = evaluation[
            head["target"]
        ].astype(int)

        if (
            y_train.nunique() < 2
            or y_eval.nunique() < 2
        ):
            continue

        model = make_pipeline(
            factory()
        )

        model.fit(
            train[features],
            y_train,
        )

        raw = model.predict_proba(
            evaluation[features]
        )[:, 1]

        result = pd.DataFrame(
            {
                "_row_id":
                    evaluation.index,

                "fold":
                    int(
                        fold["fold"]
                    ),

                "document_no":
                    evaluation[
                        "document_no"
                    ].values,

                "posting_date":
                    evaluation[
                        POSTING_DATE
                    ].values,

                "amount":
                    pd.to_numeric(
                        evaluation["amount"],
                        errors="coerce",
                    ).values,

                "actual":
                    y_eval.values,

                "raw_probability":
                    raw,
            }
        )

        output.append(result)

    if not output:
        raise RuntimeError(
            f"No OOF predictions for {head_name}"
        )

    return pd.concat(
        output,
        ignore_index=True,
    )


def sequential_calibration_report(
    oof: pd.DataFrame,
):
    folds = sorted(
        oof["fold"].unique()
    )

    rows = []

    for fold in folds[1:]:
        history = oof[
            oof["fold"] < fold
        ]

        current = oof[
            oof["fold"] == fold
        ]

        if (
            history["actual"].sum() < 20
            or (
                len(history)
                - history["actual"].sum()
            ) < 20
        ):
            continue

        calibrator = (
            fit_probability_calibrator(
                history[
                    "raw_probability"
                ],
                history["actual"],
            )
        )

        calibrated = apply_calibrator(
            calibrator,
            current[
                "raw_probability"
            ],
        )

        raw_metrics = classifier_metrics(
            current["actual"],
            current[
                "raw_probability"
            ],
        )

        calibrated_metrics = (
            classifier_metrics(
                current["actual"],
                calibrated,
            )
        )

        rows.append(
            {
                "fold":
                    int(fold),

                "calibrator":
                    calibrator["kind"],

                "raw":
                    raw_metrics,

                "calibrated":
                    calibrated_metrics,
            }
        )

    return rows


# ============================================================
# QUANTILE OOF
# ============================================================

def quantile_oof(
    dev,
    folds,
    label: str,
):
    metadata = load_metadata(
        f"days_late_{label}"
    )

    q = float(
        metadata["quantile"]
    )

    features = metadata[
        "features"
    ]

    output = []

    for fold in folds:
        eval_start = utc(
            fold["eval_start"]
        )

        eval_end = (
            utc(
                fold["eval_end"]
            )
            + pd.Timedelta(
                days=1
            )
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
            GradientBoostingRegressor(
                loss="quantile",
                alpha=q,
                n_estimators=450,
                learning_rate=0.03,
                max_depth=3,
                min_samples_leaf=25,
                subsample=0.85,
                random_state=42,
            )
        )

        model.fit(
            train[features],
            train[
                "target_days_late_if_late"
            ],
        )

        prediction = np.maximum(
            model.predict(
                evaluation[features]
            ),
            0,
        )

        output.append(
            pd.DataFrame(
                {
                    "_row_id":
                        evaluation.index,

                    "fold":
                        int(
                            fold["fold"]
                        ),

                    "actual":
                        evaluation[
                            "target_days_late_if_late"
                        ].values,

                    "raw_prediction":
                        prediction,
                }
            )
        )

    return pd.concat(
        output,
        ignore_index=True,
    )


def calibrate_quantile(
    oof,
    quantile,
):
    residual = (
        oof["actual"]
        - oof[
            "raw_prediction"
        ]
    )

    adjustment = float(
        np.quantile(
            residual,
            quantile,
        )
    )

    calibrated = np.maximum(
        oof[
            "raw_prediction"
        ]
        + adjustment,
        0,
    )

    raw_coverage = float(
        (
            oof["actual"]
            <= oof[
                "raw_prediction"
            ]
        ).mean()
    )

    calibrated_coverage = float(
        (
            oof["actual"]
            <= calibrated
        ).mean()
    )

    return {
        "quantile":
            quantile,

        "adjustment_days":
            adjustment,

        "raw_coverage":
            raw_coverage,

        "calibrated_coverage":
            calibrated_coverage,

        "raw_pinball":
            float(
                mean_pinball_loss(
                    oof["actual"],
                    oof[
                        "raw_prediction"
                    ],
                    alpha=quantile,
                )
            ),

        "calibrated_pinball":
            float(
                mean_pinball_loss(
                    oof["actual"],
                    calibrated,
                    alpha=quantile,
                )
            ),
    }


# ============================================================
# BUSINESS METRICS
# ============================================================

def top_k_metrics(
    frame,
    score_column,
    k,
):
    ranked = frame.sort_values(
        score_column,
        ascending=False,
    )

    selected = ranked.head(
        min(
            k,
            len(ranked),
        )
    )

    actual_positive = (
        frame["actual_late"]
        == 1
    )

    selected_positive = (
        selected["actual_late"]
        == 1
    )

    prevalence = float(
        actual_positive.mean()
    )

    precision = float(
        selected_positive.mean()
    )

    recall = float(
        selected_positive.sum()
        / max(
            actual_positive.sum(),
            1,
        )
    )

    total_risky_cash = float(
        frame[
            "actual_risky_cash"
        ].sum()
    )

    captured_cash = float(
        selected[
            "actual_risky_cash"
        ].sum()
    )

    cash_capture = (
        captured_cash
        / total_risky_cash
        if total_risky_cash > 0
        else 0.0
    )

    return {
        "k":
            int(
                len(selected)
            ),

        "precision":
            precision,

        "recall":
            recall,

        "lift":
            (
                precision / prevalence
                if prevalence > 0
                else None
            ),

        "risky_cash_capture":
            cash_capture,

        "actual_risky_cash_captured":
            captured_cash,

        "selected_invoice_value":
            float(
                selected[
                    "amount"
                ].sum()
            ),
    }


# ============================================================
# MAIN
# ============================================================

def main():
    CALIBRATION_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    banner(
        "RECOVERIQ — B2B V2 CALIBRATION"
    )

    dev = add_targets(
        load_development_data()
    )

    folds = build_temporal_folds(
        dev
    )

    print(
        "Development rows:",
        f"{len(dev):,}",
    )

    print(
        "TEST SPLIT WAS NOT READ."
    )

    classifier_frames = {}

    calibration_report = {}

    for head_name in [
        "late_payment",
        "delay_over_7d",
        "delay_over_15d",
    ]:
        banner(
            f"CALIBRATE — {head_name.upper()}"
        )

        oof = classifier_oof(
            dev,
            folds,
            head_name,
        )

        final_calibrator = (
            fit_probability_calibrator(
                oof[
                    "raw_probability"
                ],
                oof["actual"],
            )
        )

        oof[
            "calibrated_probability"
        ] = apply_calibrator(
            final_calibrator,
            oof[
                "raw_probability"
            ],
        )

        before = classifier_metrics(
            oof["actual"],
            oof[
                "raw_probability"
            ],
        )

        after = classifier_metrics(
            oof["actual"],
            oof[
                "calibrated_probability"
            ],
        )

        temporal_report = (
            sequential_calibration_report(
                oof
            )
        )

        print(
            "Calibrator:",
            final_calibrator[
                "kind"
            ],
        )

        print(
            "Brier:",
            f"{before['brier']:.4f}",
            "→",
            f"{after['brier']:.4f}",
        )

        print(
            "ECE:",
            f"{before['ece_10']:.4f}",
            "→",
            f"{after['ece_10']:.4f}",
        )

        model_dir = (
            MODEL_ROOT
            / head_name
        )

        joblib.dump(
            final_calibrator,
            model_dir
            / "calibrator.joblib",
        )

        calibration_metadata = {
            "head":
                head_name,

            "calibrator":
                final_calibrator[
                    "kind"
                ],

            "development_rows":
                int(
                    len(oof)
                ),

            "before":
                before,

            "after":
                after,

            "sequential_temporal_backtest":
                temporal_report,

            "test_used":
                False,
        }

        (
            model_dir
            / "calibration.json"
        ).write_text(
            json.dumps(
                calibration_metadata,
                indent=2,
            )
        )

        calibration_report[
            head_name
        ] = (
            calibration_metadata
        )

        classifier_frames[
            head_name
        ] = oof

    # --------------------------------------------------------
    # Quantile calibration
    # --------------------------------------------------------

    banner(
        "QUANTILE CALIBRATION"
    )

    quantile_calibration = {}

    for label, q in [
        ("p80", 0.80),
    ]:
        oof = quantile_oof(
            dev,
            folds,
            label,
        )

        result = calibrate_quantile(
            oof,
            q,
        )

        quantile_calibration[
            label
        ] = result

        print(
            f"{label.upper():<6}"
            f"adjustment="
            f"{result['adjustment_days']:+.3f}d  "
            f"coverage "
            f"{result['raw_coverage']:.3f}"
            f" → "
            f"{result['calibrated_coverage']:.3f}"
        )

    QUANTILE_PATH.write_text(
        json.dumps(
            quantile_calibration,
            indent=2,
        )
    )

    # --------------------------------------------------------
    # Merge OOF heads
    # --------------------------------------------------------

    late = classifier_frames[
        "late_payment"
    ][
        [
            "_row_id",
            "document_no",
            "posting_date",
            "amount",
            "actual",
            "calibrated_probability",
        ]
    ].rename(
        columns={
            "actual":
                "actual_late",

            "calibrated_probability":
                "p_late",
        }
    )

    p7 = classifier_frames[
        "delay_over_7d"
    ][
        [
            "_row_id",
            "calibrated_probability",
        ]
    ].rename(
        columns={
            "calibrated_probability":
                "p_over_7d"
        }
    )

    p15 = classifier_frames[
        "delay_over_15d"
    ][
        [
            "_row_id",
            "calibrated_probability",
        ]
    ].rename(
        columns={
            "calibrated_probability":
                "p_over_15d"
        }
    )

    business = (
        late.merge(
            p7,
            on="_row_id",
            how="inner",
        )
        .merge(
            p15,
            on="_row_id",
            how="inner",
        )
    )

    # --------------------------------------------------------
    # Enforce logical probability hierarchy
    #
    # P(late) >= P(>7d) >= P(>15d)
    # --------------------------------------------------------

    business[
        "p_over_7d"
    ] = np.minimum(
        business[
            "p_over_7d"
        ],
        business[
            "p_late"
        ],
    )

    business[
        "p_over_15d"
    ] = np.minimum(
        business[
            "p_over_15d"
        ],
        business[
            "p_over_7d"
        ],
    )

    business["amount"] = (
        pd.to_numeric(
            business["amount"],
            errors="coerce",
        )
        .fillna(0)
        .clip(lower=0)
    )

    business[
        "expected_overdue_exposure"
    ] = (
        business["amount"]
        * business["p_late"]
    )

    business[
        "actual_risky_cash"
    ] = (
        business["amount"]
        * business[
            "actual_late"
        ]
    )

    # --------------------------------------------------------
    # Business ranking metrics
    # --------------------------------------------------------

    banner(
        "BUSINESS RANKING METRICS"
    )

    ranking_report = {}

    for ranking in [
        "p_late",
        "expected_overdue_exposure",
    ]:
        print()
        print(
            "Ranking:",
            ranking,
        )

        ranking_report[
            ranking
        ] = {}

        for k in [
            50,
            100,
            250,
            500,
            1000,
        ]:
            metrics = top_k_metrics(
                business,
                ranking,
                k,
            )

            ranking_report[
                ranking
            ][
                str(k)
            ] = metrics

            print(
                f"Top {k:<4} "
                f"precision="
                f"{metrics['precision']:.2%}  "
                f"recall="
                f"{metrics['recall']:.2%}  "
                f"cash_capture="
                f"{metrics['risky_cash_capture']:.2%}"
            )

    business.to_csv(
        OOF_PATH,
        index=False,
        compression="gzip",
    )

    BUSINESS_METRICS_PATH.write_text(
        json.dumps(
            {
                "classification_calibration":
                    calibration_report,

                "ranking":
                    ranking_report,

                "test_used":
                    False,
            },
            indent=2,
        )
    )

    # --------------------------------------------------------
    # Agent QA contract
    # --------------------------------------------------------

    contract = {
        "version":
            "b2b_v2",

        "supported_questions": {
            "will_invoice_be_late": {
                "answer":
                    "late_probability",

                "model_status":
                    "supported",
            },

            "will_invoice_clear_by_due_date": {
                "answer":
                    "clear_by_due_probability",

                "formula":
                    "1 - late_probability",
            },

            "will_invoice_be_more_than_7_days_late": {
                "answer":
                    "delay_over_7d_probability",

                "model_status":
                    "supported",
            },

            "will_invoice_clear_by_due_plus_7_days": {
                "answer":
                    "clear_by_due_plus_7d_probability",

                "formula":
                    "1 - delay_over_7d_probability",
            },

            "will_invoice_be_more_than_15_days_late": {
                "answer":
                    "delay_over_15d_probability",

                "model_status":
                    "experimental",
            },

            "will_invoice_clear_by_due_plus_15_days": {
                "answer":
                    "clear_by_due_plus_15d_probability",

                "model_status":
                    "experimental",
            },

            "if_late_how_many_days": {
                "answers": [
                    "conditional_expected_days_late",
                    "conservative_clearance_window_days",
                ]
            },

            "what_is_expected_delay": {
                "answer":
                    "expected_days_late",
            },

            "how_much_cash_is_at_risk": {
                "answer":
                    "expected_overdue_exposure",
            },

            "should_we_intervene": {
                "answer":
                    "recommended_action",

                "source":
                    "deterministic policy",
            },

            "how_urgent_is_it": {
                "answers": [
                    "risk_tier",
                    "recommended_action",
                ]
            },

            "when_might_it_clear_if_late": {
                "answers": [
                    "conditional_expected_clearance_date",
                    "conservative_clearance_date",
                ]
            },
        },

        "unsupported_from_b2b_v2": {
            "will_it_ever_be_recovered":
                (
                    "Dataset does not provide a reliable "
                    "non-recovery population."
                ),

            "true_time_to_clear_probability_curve":
                (
                    "Use BPI survival/time-to-event model."
                ),

            "probability_clear_within_7_days_from_today":
                (
                    "Use BPI survival/time-to-event model."
                ),

            "causal_effect_of_contacting_customer":
                (
                    "Requires historical intervention/control data."
                ),

            "why_model_predicted_risk":
                (
                    "Requires SHAP/explainability layer; "
                    "do not infer reasons from probability alone."
                ),
        },
    }

    AGENT_CONTRACT_PATH.write_text(
        json.dumps(
            contract,
            indent=2,
        )
    )

    banner(
        "CALIBRATION COMPLETE"
    )

    print(
        "OOF predictions:"
    )
    print(
        " ",
        OOF_PATH,
    )

    print(
        "\nBusiness metrics:"
    )
    print(
        " ",
        BUSINESS_METRICS_PATH,
    )

    print(
        "\nQuantile calibration:"
    )
    print(
        " ",
        QUANTILE_PATH,
    )

    print(
        "\nAgent QA contract:"
    )
    print(
        " ",
        AGENT_CONTRACT_PATH,
    )

    print()
    print(
        "TEST SPLIT WAS NEVER READ."
    )


if __name__ == "__main__":
    main()
