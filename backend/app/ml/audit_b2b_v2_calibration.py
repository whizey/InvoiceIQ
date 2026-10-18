"""
RecoverIQ — Honest Temporal Calibration Audit
==============================================

Purpose:
Evaluate calibration exactly as it would have behaved historically.

For each temporal fold:
    calibration model is fitted ONLY on earlier folds
    and evaluated on the next unseen fold.

The reserved TEST split is NEVER read.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    add_targets,
    build_temporal_folds,
    classifier_metrics,
    load_development_data,
)

from app.ml.calibrate_b2b_v2 import (
    apply_calibrator,
    classifier_oof,
    fit_probability_calibrator,
    quantile_oof,
    top_k_metrics,
)


OUTPUT_DIR = (
    ARTIFACT_ROOT
    / "calibration"
)

REPORT_PATH = (
    OUTPUT_DIR
    / "temporal_calibration_audit.json"
)

BUSINESS_PATH = (
    OUTPUT_DIR
    / "temporal_business_metrics.json"
)


def banner(title):
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


# ============================================================
# HONEST CLASSIFIER CALIBRATION
# ============================================================

def sequential_classifier_calibration(
    oof: pd.DataFrame,
):
    """
    Fold N may only use folds < N
    to fit its probability calibrator.
    """

    output = []

    folds = sorted(
        oof["fold"].unique()
    )

    for fold in folds[1:]:

        history = oof[
            oof["fold"] < fold
        ].copy()

        current = oof[
            oof["fold"] == fold
        ].copy()

        positives = int(
            history["actual"].sum()
        )

        negatives = int(
            len(history) - positives
        )

        if (
            positives < 20
            or negatives < 20
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

        current[
            "temporal_calibrated_probability"
        ] = apply_calibrator(
            calibrator,
            current[
                "raw_probability"
            ],
        )

        current[
            "calibrator_kind"
        ] = calibrator["kind"]

        output.append(
            current
        )

    if not output:
        return pd.DataFrame()

    return pd.concat(
        output,
        ignore_index=True,
    )


# ============================================================
# HONEST QUANTILE CALIBRATION
# ============================================================

def sequential_quantile_calibration(
    oof: pd.DataFrame,
    q: float,
):
    """
    Quantile adjustment learned only from
    historical folds.
    """

    output = []

    folds = sorted(
        oof["fold"].unique()
    )

    for fold in folds[1:]:

        history = oof[
            oof["fold"] < fold
        ].copy()

        current = oof[
            oof["fold"] == fold
        ].copy()

        residual = (
            history["actual"]
            - history[
                "raw_prediction"
            ]
        )

        adjustment = float(
            np.quantile(
                residual,
                q,
            )
        )

        current[
            "calibrated_prediction"
        ] = np.maximum(
            current[
                "raw_prediction"
            ]
            + adjustment,
            0,
        )

        current[
            "adjustment_days"
        ] = adjustment

        output.append(
            current
        )

    if not output:
        raise RuntimeError(
            "No sequential quantile rows."
        )

    return pd.concat(
        output,
        ignore_index=True,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    banner(
        "RECOVERIQ — HONEST TEMPORAL CALIBRATION"
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

    classifier_report = {}

    sequential_heads = {}

    # ========================================================
    # CLASSIFICATION
    # ========================================================

    for head in [
        "late_payment",
        "delay_over_7d",
        "delay_over_15d",
    ]:

        banner(
            f"TEMPORAL CALIBRATION — "
            f"{head.upper()}"
        )

        oof = classifier_oof(
            dev,
            folds,
            head,
        )

        sequential = (
            sequential_classifier_calibration(
                oof
            )
        )

        if sequential.empty:
            print(
                "Insufficient historical positives/negatives "
                "for honest sequential calibration."
            )
            print(
                "Status: EXPERIMENTAL / UNCALIBRATED TEMPORALLY"
            )

            classifier_report[
                head
            ] = {
                "status":
                    "insufficient_temporal_calibration_support",

                "oof_rows":
                    int(len(oof)),

                "positives":
                    int(oof["actual"].sum()),

                "negative":
                    int(
                        len(oof)
                        - oof["actual"].sum()
                    ),

                "test_used":
                    False,
            }

            sequential_heads[
                head
            ] = None

            continue

        raw_metrics = (
            classifier_metrics(
                sequential[
                    "actual"
                ],
                sequential[
                    "raw_probability"
                ],
            )
        )

        calibrated_metrics = (
            classifier_metrics(
                sequential[
                    "actual"
                ],
                sequential[
                    "temporal_calibrated_probability"
                ],
            )
        )

        print(
            f"Rows: "
            f"{len(sequential):,}"
        )

        print(
            "Brier:",
            f"{raw_metrics['brier']:.4f}",
            "→",
            f"{calibrated_metrics['brier']:.4f}",
        )

        print(
            "ECE:",
            f"{raw_metrics['ece_10']:.4f}",
            "→",
            f"{calibrated_metrics['ece_10']:.4f}",
        )

        print(
            "ROC-AUC:",
            f"{raw_metrics['roc_auc']:.4f}",
            "→",
            f"{calibrated_metrics['roc_auc']:.4f}",
        )

        print(
            "PR-AUC:",
            f"{raw_metrics['pr_auc']:.4f}",
            "→",
            f"{calibrated_metrics['pr_auc']:.4f}",
        )

        classifier_report[
            head
        ] = {
            "raw":
                raw_metrics,

            "temporally_calibrated":
                calibrated_metrics,

            "rows":
                int(
                    len(sequential)
                ),

            "test_used":
                False,
        }

        sequential_heads[
            head
        ] = sequential

    # ========================================================
    # QUANTILE AUDIT
    # ========================================================

    banner(
        "TEMPORAL QUANTILE CALIBRATION"
    )

    quantile_report = {}

    for label, q in [
        ("p80", 0.80),
    ]:

        oof = quantile_oof(
            dev,
            folds,
            label,
        )

        sequential = (
            sequential_quantile_calibration(
                oof,
                q,
            )
        )

        raw_coverage = float(
            (
                sequential["actual"]
                <= sequential[
                    "raw_prediction"
                ]
            ).mean()
        )

        calibrated_coverage = float(
            (
                sequential["actual"]
                <= sequential[
                    "calibrated_prediction"
                ]
            ).mean()
        )

        mean_adjustment = float(
            sequential[
                "adjustment_days"
            ].mean()
        )

        print(
            f"{label.upper():<6}"
            f"raw coverage="
            f"{raw_coverage:.3f}  "
            f"temporal calibrated="
            f"{calibrated_coverage:.3f}  "
            f"target={q:.2f}  "
            f"avg adjustment="
            f"{mean_adjustment:+.2f}d"
        )

        quantile_report[
            label
        ] = {
            "target_coverage":
                q,

            "raw_coverage":
                raw_coverage,

            "temporal_calibrated_coverage":
                calibrated_coverage,

            "coverage_error":
                abs(
                    calibrated_coverage
                    - q
                ),

            "mean_adjustment_days":
                mean_adjustment,
        }

    # ========================================================
    # HONEST BUSINESS RANKING
    # ========================================================

    banner(
        "TEMPORAL BUSINESS VALUE"
    )

    late = sequential_heads[
        "late_payment"
    ][
        [
            "_row_id",
            "document_no",
            "posting_date",
            "amount",
            "actual",
            "temporal_calibrated_probability",
        ]
    ].rename(
        columns={
            "actual":
                "actual_late",

            "temporal_calibrated_probability":
                "p_late",
        }
    )

    p7 = sequential_heads[
        "delay_over_7d"
    ][
        [
            "_row_id",
            "temporal_calibrated_probability",
        ]
    ].rename(
        columns={
            "temporal_calibrated_probability":
                "p_over_7d",
        }
    )

    p15_frame = sequential_heads.get(
        "delay_over_15d"
    )

    business = (
        late
        .merge(
            p7,
            on="_row_id",
            how="inner",
        )
    )

    if (
        p15_frame is not None
        and not p15_frame.empty
    ):
        p15 = p15_frame[
            [
                "_row_id",
                "temporal_calibrated_probability",
            ]
        ].rename(
            columns={
                "temporal_calibrated_probability":
                    "p_over_15d",
            }
        )

        business = business.merge(
            p15,
            on="_row_id",
            how="inner",
        )

    else:
        business[
            "p_over_15d"
        ] = np.nan

    # Logical hierarchy.
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
    ] = np.where(
        business[
            "p_over_15d"
        ].notna(),

        np.minimum(
            business[
                "p_over_15d"
            ],
            business[
                "p_over_7d"
            ],
        ),

        np.nan,
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

    # ========================================================
    # SAVE
    # ========================================================

    report = {
        "classification":
            classifier_report,

        "quantiles":
            quantile_report,

        "test_split_read":
            False,

        "evaluation_type":
            (
                "strict sequential temporal "
                "calibration evaluation"
            ),
    }

    REPORT_PATH.write_text(
        json.dumps(
            report,
            indent=2,
        )
    )

    BUSINESS_PATH.write_text(
        json.dumps(
            {
                "ranking":
                    ranking_report,

                "evaluation_rows":
                    int(
                        len(
                            business
                        )
                    ),

                "test_split_read":
                    False,
            },
            indent=2,
        )
    )

    banner(
        "HONEST TEMPORAL AUDIT COMPLETE"
    )

    print(
        "Calibration report:"
    )

    print(
        " ",
        REPORT_PATH,
    )

    print(
        "\nBusiness report:"
    )

    print(
        " ",
        BUSINESS_PATH,
    )

    print()
    print(
        "TEST SPLIT WAS NEVER READ."
    )


if __name__ == "__main__":
    main()
