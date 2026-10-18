"""
RecoverIQ — B2B v2 Target & Feature Contract Audit

Purpose
-------
Before training B2B v2, determine which business targets are:

1. correctly defined,
2. sufficiently represented,
3. reasonably stable through time,
4. suitable for classification/regression,
5. free from obvious time-proxy features.

IMPORTANT
---------
The reserved test split is NEVER read by this script.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]

SPLIT_DIR = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "primary"
    / "splits"
)

OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "artifacts"
    / "b2b_v2"
)

OUTPUT_PATH = (
    OUTPUT_DIR
    / "target_feature_contract.json"
)


# ============================================================
# SOURCE TARGETS
# ============================================================

LATE_FLAG = "late_payment_weekend_adjusted"

DAYS_LATE_SOURCE = (
    "days_late_weekend_adjusted"
)


# ============================================================
# V2 CORE FEATURES
# ============================================================

# customer_tenure_days is intentionally NOT part of
# the core set because previous audit showed Spearman
# correlation ≈ 0.944 with calendar time.
#
# It will be evaluated separately as an ablation feature.

CORE_FEATURES = [
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

    "days_since_previous_invoice",

    "recent_5_late_rate",
    "recent_5_avg_days_late",

    "recent_late_streak",
    "recent_on_time_streak",

    "recent_5_late_rate_delta",
    "recent_5_avg_days_late_delta",
]


ABLATION_FEATURES = [
    "customer_tenure_days",
]


# ============================================================
# HELPERS
# ============================================================

def banner(title: str) -> None:
    print()
    print("=" * 84)
    print(title)
    print("=" * 84)


def load_split(name: str) -> pd.DataFrame:

    path = (
        SPLIT_DIR
        / f"{name}.csv.gz"
    )

    if not path.exists():
        raise FileNotFoundError(
            path
        )

    return pd.read_csv(
        path,
        parse_dates=[
            "posting_date",
        ],
    )


# ============================================================
# TARGET ENGINEERING
# ============================================================

def add_v2_targets(
    frame: pd.DataFrame,
) -> pd.DataFrame:

    df = frame.copy()

    if LATE_FLAG not in df:
        raise ValueError(
            f"Missing {LATE_FLAG}"
        )

    if DAYS_LATE_SOURCE not in df:
        raise ValueError(
            f"Missing {DAYS_LATE_SOURCE}"
        )

    raw_days = pd.to_numeric(
        df[DAYS_LATE_SOURCE],
        errors="coerce",
    )

    # Business lateness cannot be negative.
    df["days_late"] = (
        raw_days
        .fillna(0)
        .clip(lower=0)
    )

    # --------------------------------------------------------
    # Head 1: misses contractual due date
    # --------------------------------------------------------

    df["target_late_payment"] = (
        df[LATE_FLAG]
        .astype(int)
    )

    # --------------------------------------------------------
    # Head 2: severity horizons
    #
    # These are defined over ALL invoices.
    # An on-time invoice is naturally 0.
    # --------------------------------------------------------

    for horizon in [
        7,
        15,
        30,
        60,
        90,
    ]:

        df[
            f"target_delay_over_{horizon}d"
        ] = (
            df["days_late"]
            > horizon
        ).astype(int)

    # --------------------------------------------------------
    # Head 3: conditional lateness regression
    # --------------------------------------------------------

    df[
        "target_days_late_if_late"
    ] = np.where(
        df[
            "target_late_payment"
        ] == 1,
        df["days_late"],
        np.nan,
    )

    # --------------------------------------------------------
    # Business severity bucket
    #
    # Primarily a DERIVED label.
    # We do not necessarily need a separate model for it.
    # --------------------------------------------------------

    conditions = [
        df["days_late"] <= 0,

        (
            (df["days_late"] > 0)
            & (df["days_late"] <= 7)
        ),

        (
            (df["days_late"] > 7)
            & (df["days_late"] <= 30)
        ),

        (
            (df["days_late"] > 30)
            & (df["days_late"] <= 60)
        ),

        df["days_late"] > 60,
    ]

    choices = [
        "ON_TIME",
        "MILD_1_7D",
        "MODERATE_8_30D",
        "HIGH_31_60D",
        "CRITICAL_60D_PLUS",
    ]

    df[
        "target_severity"
    ] = np.select(
        conditions,
        choices,
        default="UNKNOWN",
    )

    return df


# ============================================================
# TARGET AUDIT
# ============================================================

def binary_target_stats(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    target: str,
) -> dict:

    train_rate = float(
        train[target].mean()
    )

    validation_rate = float(
        validation[target].mean()
    )

    train_positive = int(
        train[target].sum()
    )

    validation_positive = int(
        validation[target].sum()
    )

    train_negative = int(
        len(train)
        - train_positive
    )

    validation_negative = int(
        len(validation)
        - validation_positive
    )

    # Basic support criterion.
    # Not an ML rule — just a guard against absurdly small targets.
    enough_support = bool(
        train_positive >= 300
        and train_negative >= 300
        and validation_positive >= 100
        and validation_negative >= 100
    )

    return {
        "train_rows":
            int(len(train)),

        "validation_rows":
            int(len(validation)),

        "train_positive":
            train_positive,

        "train_negative":
            train_negative,

        "validation_positive":
            validation_positive,

        "validation_negative":
            validation_negative,

        "train_rate":
            train_rate,

        "validation_rate":
            validation_rate,

        "absolute_rate_shift":
            abs(
                validation_rate
                - train_rate
            ),

        "enough_support":
            enough_support,
    }


def print_binary_target(
    target: str,
    stats: dict,
) -> None:

    verdict = (
        "SUPPORTED"
        if stats[
            "enough_support"
        ]
        else "LOW SUPPORT"
    )

    print(
        f"{target:<30}"
        f"train={stats['train_rate']:>7.2%}  "
        f"val={stats['validation_rate']:>7.2%}  "
        f"Δ={stats['absolute_rate_shift']:>7.2%}  "
        f"val+={stats['validation_positive']:>5,}  "
        f"{verdict}"
    )


# ============================================================
# MONTHLY TARGET DRIFT
# ============================================================

def monthly_target_report(
    combined: pd.DataFrame,
    targets: list[str],
) -> pd.DataFrame:

    df = combined.copy()

    df["month"] = (
        df["posting_date"]
        .dt.to_period("M")
        .astype(str)
    )

    aggregations = {
        "rows":
            ("document_no", "size"),
    }

    for target in targets:
        aggregations[
            target
        ] = (
            target,
            "mean",
        )

    return (
        df.groupby("month")
        .agg(**aggregations)
    )


# ============================================================
# FEATURE TIME-PROXY AUDIT
# ============================================================

def feature_time_proxy_audit(
    combined: pd.DataFrame,
) -> dict:

    frame = combined.copy()

    frame["posting_ordinal"] = (
        frame["posting_date"]
        .map(
            pd.Timestamp.toordinal
        )
    )

    results = {}

    for feature in (
        CORE_FEATURES
        + ABLATION_FEATURES
    ):

        if feature not in frame.columns:
            continue

        values = pd.to_numeric(
            frame[feature],
            errors="coerce",
        )

        valid = (
            values.notna()
            & frame[
                "posting_ordinal"
            ].notna()
        )

        if valid.sum() < 10:
            continue

        time_corr = (
            pd.DataFrame(
                {
                    "feature":
                        values[valid],

                    "time":
                        frame.loc[
                            valid,
                            "posting_ordinal",
                        ],
                }
            )
            .corr(
                method="spearman"
            )
            .iloc[0, 1]
        )

        late_corr = (
            pd.DataFrame(
                {
                    "feature":
                        values[valid],

                    "late":
                        frame.loc[
                            valid,
                            "target_late_payment",
                        ],
                }
            )
            .corr(
                method="spearman"
            )
            .iloc[0, 1]
        )

        results[feature] = {
            "time_spearman":
                None
                if pd.isna(time_corr)
                else float(time_corr),

            "late_target_spearman":
                None
                if pd.isna(late_corr)
                else float(late_corr),
        }

    return results


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    banner(
        "RECOVERIQ — B2B V2 TARGET CONTRACT"
    )

    train = add_v2_targets(
        load_split("train")
    )

    validation = add_v2_targets(
        load_split("validation")
    )

    print(
        f"Train:      {len(train):,}"
    )

    print(
        f"Validation: {len(validation):,}"
    )

    print()
    print(
        "TEST SPLIT WAS NOT READ."
    )

    binary_targets = [
        "target_late_payment",
        "target_delay_over_7d",
        "target_delay_over_15d",
        "target_delay_over_30d",
        "target_delay_over_60d",
        "target_delay_over_90d",
    ]

    # --------------------------------------------------------
    # Binary target support
    # --------------------------------------------------------

    banner(
        "1. CLASSIFICATION TARGET SUPPORT"
    )

    binary_stats = {}

    for target in binary_targets:

        stats = binary_target_stats(
            train,
            validation,
            target,
        )

        binary_stats[
            target
        ] = stats

        print_binary_target(
            target,
            stats,
        )

    # --------------------------------------------------------
    # Regression target
    # --------------------------------------------------------

    banner(
        "2. CONDITIONAL LATE-DAYS TARGET"
    )

    train_late = (
        train[
            train[
                "target_late_payment"
            ] == 1
        ]
    )

    validation_late = (
        validation[
            validation[
                "target_late_payment"
            ] == 1
        ]
    )

    print(
        "\nTRAIN — late invoices only"
    )

    print(
        train_late[
            "target_days_late_if_late"
        ]
        .describe(
            percentiles=[
                .25,
                .50,
                .75,
                .80,
                .90,
                .95,
                .99,
            ]
        )
        .round(2)
        .to_string()
    )

    print(
        "\nVALIDATION — late invoices only"
    )

    print(
        validation_late[
            "target_days_late_if_late"
        ]
        .describe(
            percentiles=[
                .25,
                .50,
                .75,
                .80,
                .90,
                .95,
                .99,
            ]
        )
        .round(2)
        .to_string()
    )

    # --------------------------------------------------------
    # Severity buckets
    # --------------------------------------------------------

    banner(
        "3. SEVERITY DISTRIBUTION"
    )

    severity_table = pd.DataFrame(
        {
            "train":
                train[
                    "target_severity"
                ].value_counts(),

            "validation":
                validation[
                    "target_severity"
                ].value_counts(),
        }
    ).fillna(0).astype(int)

    severity_table[
        "train_pct"
    ] = (
        severity_table["train"]
        / len(train)
    )

    severity_table[
        "validation_pct"
    ] = (
        severity_table[
            "validation"
        ]
        / len(validation)
    )

    print(
        severity_table
        .sort_index()
        .to_string()
    )

    # --------------------------------------------------------
    # Monthly drift
    # --------------------------------------------------------

    banner(
        "4. MONTHLY TARGET DRIFT"
    )

    combined = pd.concat(
        [
            train.assign(
                split="train"
            ),
            validation.assign(
                split="validation"
            ),
        ],
        ignore_index=True,
    )

    monthly = monthly_target_report(
        combined,
        binary_targets,
    )

    printable = monthly.copy()

    for target in binary_targets:
        printable[target] = (
            printable[target]
            * 100
        ).round(2)

    print(
        printable.to_string()
    )

    # --------------------------------------------------------
    # Time-proxy feature audit
    # --------------------------------------------------------

    banner(
        "5. FEATURE TIME-PROXY AUDIT"
    )

    feature_audit = (
        feature_time_proxy_audit(
            combined
        )
    )

    ranked = sorted(
        feature_audit.items(),
        key=lambda item: abs(
            item[1][
                "time_spearman"
            ] or 0
        ),
        reverse=True,
    )

    for feature, values in ranked:

        time_corr = values[
            "time_spearman"
        ]

        late_corr = values[
            "late_target_spearman"
        ]

        marker = ""

        if (
            time_corr is not None
            and abs(time_corr) >= .70
        ):
            marker = "  ⚠ STRONG TIME PROXY"

        print(
            f"{feature:<40}"
            f"time={time_corr if time_corr is not None else float('nan'):>8.3f}  "
            f"late={late_corr if late_corr is not None else float('nan'):>8.3f}"
            f"{marker}"
        )

    # --------------------------------------------------------
    # Final contract
    # --------------------------------------------------------

    banner(
        "6. B2B V2 MODEL CONTRACT"
    )

    recommended_binary_targets = [
        target
        for target, stats
        in binary_stats.items()
        if stats["enough_support"]
    ]

    contract = {
        "version":
            "b2b_v2",

        "prediction_point":
            "invoice/posting time",

        "core_features":
            CORE_FEATURES,

        "ablation_only_features":
            ABLATION_FEATURES,

        "classification_targets":
            binary_stats,

        "recommended_binary_targets":
            recommended_binary_targets,

        "regression_target": {
            "name":
                "target_days_late_if_late",

            "population":
                "late invoices only",

            "training_objectives": [
                "conditional mean regression",
                "quantile P80 for a conservative clearance window",
            ],
        },

        "derived_outputs": [
            "expected_days_late",
            "expected_overdue_exposure",
            "risk_tier",
            "collection_priority_score",
            "intervention_recommendation",
            "escalation_level",
        ],

        "evaluation": {
            "late_risk": [
                "PR-AUC",
                "ROC-AUC",
                "Brier score",
                "log loss",
                "calibration",
                "temporal fold stability",
            ],

            "delay_horizons": [
                "PR-AUC",
                "ROC-AUC",
                "Brier score",
                "calibration",
            ],

            "delay_regression": [
                "MAE",
                "median absolute error",
                "RMSE",
                "pinball loss",
                "quantile coverage",
            ],

            "business": [
                "Precision@K",
                "Recall@K",
                "cash-at-risk captured@K",
            ],
        },

        "test_split_read":
            False,

        "notes": [
            (
                "Existing v1 models remain baseline artifacts."
            ),
            (
                "customer_tenure_days is excluded from the "
                "core model and retained only for controlled ablation."
            ),
            (
                "Derived business decisions are not trained as "
                "independent ML targets without intervention labels."
            ),
        ],
    }

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_PATH.write_text(
        json.dumps(
            contract,
            indent=2,
        )
    )

    print(
        "\nRecommended binary heads:"
    )

    for target in (
        recommended_binary_targets
    ):
        print(
            "  ✓",
            target,
        )

    print(
        "\nCore feature count:",
        len(CORE_FEATURES),
    )

    print(
        "Ablation-only:",
        ABLATION_FEATURES,
    )

    print(
        "\nSaved:"
    )

    print(
        " ",
        OUTPUT_PATH,
    )

    print()
    print(
        "TEST SPLIT REMAINS UNTOUCHED."
    )

    banner(
        "B2B V2 TARGET AUDIT COMPLETE"
    )


if __name__ == "__main__":
    main()
