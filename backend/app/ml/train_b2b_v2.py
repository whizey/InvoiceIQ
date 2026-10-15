"""
RecoverIQ — B2B v2 Multi-Target Recovery Intelligence
======================================================

Business outputs
----------------
1. P(invoice misses due date)
2. P(invoice becomes >7 days late)
3. P(invoice becomes >15 days late)
4. Expected days late IF late
5. Conservative clearance window IF late (P80 internally)

Derived later:
- expected days late
- expected overdue exposure
- severity
- collection priority
- intervention recommendation
- escalation level

Model-development policy
------------------------
- Train + validation are the DEVELOPMENT population.
- Reserved TEST split is NEVER read.
- Expanding chronological backtesting.
- Outcome-availability purge at every temporal fold.
- Whole dates stay together.
- Feature-set ablation.
- Stability matters more than peak AUC.
- Models are refit only after configuration selection.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import TransformedTargetRegressor
from sklearn.ensemble import (
    GradientBoostingClassifier,
    GradientBoostingRegressor,
    RandomForestClassifier,
    RandomForestRegressor,
)
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    mean_absolute_error,
    mean_pinball_loss,
    mean_squared_error,
    median_absolute_error,
    r2_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline

from xgboost import (
    XGBClassifier,
    XGBRegressor,
)


# ============================================================
# PATHS
# ============================================================

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
    / "b2b_v2"
)

MODEL_ROOT = (
    ARTIFACT_ROOT
    / "models"
)

REPORT_ROOT = (
    ARTIFACT_ROOT
    / "reports"
)


# ============================================================
# SOURCE COLUMNS
# ============================================================

POSTING_DATE = "posting_date"
DUE_DATE = "net_due_date"

LATE_FLAG = (
    "late_payment_weekend_adjusted"
)

DAYS_LATE = (
    "days_late_weekend_adjusted"
)


# ============================================================
# FEATURE SETS
# ============================================================

# customer_tenure_days intentionally removed:
# previous audit showed time correlation ≈ 0.944.

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


NO_OPEN_RATIO_FEATURES = [
    x
    for x in CORE_FEATURES
    if x != "open_invoice_ratio"
]


BEHAVIOUR_HEAVY_FEATURES = [
    "amount",
    "payment_term",

    "prior_open_invoice_count",
    "has_resolved_history",

    "historical_late_rate",
    "historical_avg_days_late",

    "amount_vs_historical_avg_ratio",
    "days_since_previous_invoice",

    "recent_5_late_rate",
    "recent_5_avg_days_late",

    "recent_late_streak",
    "recent_on_time_streak",

    "recent_5_late_rate_delta",
    "recent_5_avg_days_late_delta",
]


FEATURE_SETS = {
    "core_17":
        CORE_FEATURES,

    "no_open_ratio":
        NO_OPEN_RATIO_FEATURES,

    "behaviour_heavy":
        BEHAVIOUR_HEAVY_FEATURES,
}


# ============================================================
# MODEL TARGETS
# ============================================================

CLASSIFICATION_TARGETS = {
    "late_payment": {
        "target":
            "target_late_payment",

        "availability":
            "available_late_payment",

        "business_question":
            "Will this invoice miss its adjusted due date?",
    },

    "delay_over_7d": {
        "target":
            "target_delay_over_7d",

        "availability":
            "available_delay_over_7d",

        "business_question":
            "Will this invoice become more than 7 days late?",
    },

    "delay_over_15d": {
        "target":
            "target_delay_over_15d",

        "availability":
            "available_delay_over_15d",

        "business_question":
            "Will this invoice become more than 15 days late?",
    },
}


# ============================================================
# TEMPORAL BACKTEST
# ============================================================

INITIAL_TRAIN_FRACTION = 0.50

FOLD_BOUNDARY_FRACTIONS = [
    0.50,
    0.625,
    0.750,
    0.875,
    1.00,
]


# ============================================================
# UTILITIES
# ============================================================

def banner(title: str) -> None:

    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def load_development_data() -> pd.DataFrame:
    """
    IMPORTANT:
    Reads train + validation only.
    Does NOT read the reserved test split.
    """

    frames = []

    for split in [
        "train",
        "validation",
    ]:

        path = (
            SPLIT_DIR
            / f"{split}.csv.gz"
        )

        if not path.exists():
            raise FileNotFoundError(
                path
            )

        frame = pd.read_csv(
            path,
            parse_dates=[
                POSTING_DATE,
                DUE_DATE,
            ],
        )

        frame["_source_split"] = split

        frames.append(
            frame
        )

    dev = pd.concat(
        frames,
        ignore_index=True,
    )

    for column in [
        POSTING_DATE,
        DUE_DATE,
    ]:
        dev[column] = pd.to_datetime(
            dev[column],
            utc=True,
            errors="coerce",
        )

    dev = (
        dev.sort_values(
            [
                POSTING_DATE,
                "document_no",
            ],
            kind="stable",
        )
        .reset_index(
            drop=True
        )
    )

    return dev


# ============================================================
# TARGET ENGINEERING
# ============================================================

def weekend_adjusted_due_date(
    due: pd.Series,
) -> pd.Series:

    adjusted = due.copy()

    weekday = (
        adjusted.dt.weekday
    )

    saturday = (
        weekday == 5
    )

    sunday = (
        weekday == 6
    )

    adjusted.loc[
        saturday
    ] = (
        adjusted.loc[
            saturday
        ]
        + pd.Timedelta(
            days=2
        )
    )

    adjusted.loc[
        sunday
    ] = (
        adjusted.loc[
            sunday
        ]
        + pd.Timedelta(
            days=1
        )
    )

    return adjusted


def earlier_timestamp(
    left: pd.Series,
    right: pd.Series,
) -> pd.Series:

    return (
        pd.concat(
            [
                left.rename("a"),
                right.rename("b"),
            ],
            axis=1,
        )
        .min(axis=1)
    )


def add_targets(
    frame: pd.DataFrame,
) -> pd.DataFrame:

    df = frame.copy()

    required = {
        LATE_FLAG,
        DAYS_LATE,
        POSTING_DATE,
        DUE_DATE,
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:
        raise ValueError(
            f"Missing required columns: "
            f"{sorted(missing)}"
        )

    df[
        "adjusted_due_date"
    ] = weekend_adjusted_due_date(
        df[DUE_DATE]
    )

    days_late = (
        pd.to_numeric(
            df[DAYS_LATE],
            errors="coerce",
        )
        .fillna(0)
        .clip(lower=0)
    )

    df[
        "target_days_late"
    ] = days_late

    # --------------------------------------------------------
    # HEAD A: late payment
    # --------------------------------------------------------

    df[
        "target_late_payment"
    ] = (
        pd.to_numeric(
            df[LATE_FLAG],
            errors="coerce",
        )
        .fillna(0)
        .astype(int)
    )

    # Outcome availability — CONSERVATIVE.
    #
    # clearing_date is intentionally absent from the modeling
    # split because it is an outcome/leakage column.
    #
    # Therefore, for historical backtesting, we only consider
    # the late-payment label available once the adjusted due
    # date has been reached.
    #
    # Some on-time invoices would actually become knowable
    # earlier when they clear, but waiting until due date is
    # conservative and prevents leakage.
    df[
        "available_late_payment"
    ] = df[
        "adjusted_due_date"
    ]

    # --------------------------------------------------------
    # HEAD B/C: severe lateness horizons
    # --------------------------------------------------------

    for horizon in [
        7,
        15,
    ]:

        target = (
            f"target_delay_over_{horizon}d"
        )

        available = (
            f"available_delay_over_{horizon}d"
        )

        horizon_date = (
            df["adjusted_due_date"]
            + pd.Timedelta(
                days=horizon
            )
        )

        df[target] = (
            df[
                "target_days_late"
            ]
            > horizon
        ).astype(int)

        # Conservative label availability.
        #
        # Because clearing_date is deliberately excluded from
        # the modeling split, wait until the complete severity
        # horizon has passed.
        #
        # Example:
        #   >7d target becomes safely available at due_date + 7d
        #   >15d target becomes safely available at due_date + 15d
        #
        # This can discard a little usable history, but it cannot
        # leak future outcome information.
        df[
            available
        ] = horizon_date

    # --------------------------------------------------------
    # CONDITIONAL DELAY TARGET
    # --------------------------------------------------------

    df[
        "target_days_late_if_late"
    ] = np.where(
        df[
            "target_late_payment"
        ] == 1,
        df[
            "target_days_late"
        ],
        np.nan,
    )

    # Exact lateness is known only after the late invoice
    # has actually resolved.
    #
    # The safe split does not contain clearing_date, so for
    # late invoices reconstruct a CONSERVATIVE availability
    # timestamp from:
    #
    #     adjusted_due_date + observed days late + 1 day
    #
    # The extra day protects against whole-day / timestamp
    # rounding assumptions.
    df[
        "available_days_late"
    ] = (
        df[
            "adjusted_due_date"
        ]
        + pd.to_timedelta(
            np.ceil(
                df[
                    "target_days_late"
                ]
            )
            + 1,
            unit="D",
        )
    )

    return df


# ============================================================
# FOLD GENERATION
# ============================================================

def closest_daily_index(
    daily: pd.DataFrame,
    target_rows: float,
    minimum: int = 0,
) -> int:

    candidates = (
        daily.loc[
            daily.index >= minimum
        ]
    )

    distance = (
        candidates[
            "cumulative_rows"
        ]
        .sub(
            target_rows
        )
        .abs()
    )

    return int(
        distance.idxmin()
    )


def build_temporal_folds(
    dev: pd.DataFrame,
) -> list[dict]:

    frame = dev.copy()

    frame["_date"] = (
        frame[POSTING_DATE]
        .dt.normalize()
    )

    daily = (
        frame.groupby(
            "_date"
        )
        .size()
        .rename(
            "rows"
        )
        .reset_index()
    )

    daily[
        "cumulative_rows"
    ] = (
        daily["rows"]
        .cumsum()
    )

    total = len(
        frame
    )

    indices = []

    minimum = 0

    for fraction in (
        FOLD_BOUNDARY_FRACTIONS
    ):

        if fraction == 1.0:
            idx = int(
                daily.index.max()
            )

        else:
            idx = (
                closest_daily_index(
                    daily,
                    total * fraction,
                    minimum=minimum,
                )
            )

        indices.append(
            idx
        )

        minimum = (
            idx + 1
        )

    folds = []

    for fold_no in range(
        len(indices) - 1
    ):

        train_end_idx = (
            indices[fold_no]
        )

        eval_end_idx = (
            indices[
                fold_no + 1
            ]
        )

        if (
            train_end_idx + 1
            > eval_end_idx
        ):
            continue

        train_end = (
            daily.loc[
                train_end_idx,
                "_date",
            ]
        )

        eval_start = (
            daily.loc[
                train_end_idx + 1,
                "_date",
            ]
        )

        eval_end = (
            daily.loc[
                eval_end_idx,
                "_date",
            ]
        )

        folds.append(
            {
                "fold":
                    fold_no,

                "train_end":
                    train_end,

                "eval_start":
                    eval_start,

                "eval_end":
                    eval_end,
            }
        )

    return folds


# ============================================================
# PREPROCESSING
# ============================================================

def make_pipeline(
    model,
) -> Pipeline:

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


# ============================================================
# CLASSIFICATION MODELS
# ============================================================

def classifier_factories() -> dict[
    str,
    Callable[[], object]
]:

    return {
        "random_forest":
            lambda: (
                RandomForestClassifier(
                    n_estimators=500,
                    max_depth=12,
                    min_samples_leaf=25,
                    max_features=0.5,
                    n_jobs=-1,
                    random_state=42,
                )
            ),

        "xgboost":
            lambda: (
                XGBClassifier(
                    n_estimators=500,
                    learning_rate=0.03,
                    max_depth=4,
                    min_child_weight=20,
                    subsample=0.85,
                    colsample_bytree=0.80,
                    reg_lambda=10.0,
                    objective="binary:logistic",
                    eval_metric="logloss",
                    random_state=42,
                    n_jobs=-1,
                )
            ),

        "gradient_boosting":
            lambda: (
                GradientBoostingClassifier(
                    n_estimators=400,
                    learning_rate=0.03,
                    max_depth=3,
                    min_samples_leaf=30,
                    subsample=0.85,
                    random_state=42,
                )
            ),
    }


# ============================================================
# PROBABILITY METRICS
# ============================================================

def expected_calibration_error(
    y_true,
    probability,
    bins: int = 10,
) -> float:

    y_true = np.asarray(
        y_true
    )

    probability = np.asarray(
        probability
    )

    boundaries = np.linspace(
        0,
        1,
        bins + 1,
    )

    error = 0.0

    for i in range(
        bins
    ):

        if i == bins - 1:
            mask = (
                (probability >= boundaries[i])
                & (probability <= boundaries[i + 1])
            )
        else:
            mask = (
                (probability >= boundaries[i])
                & (probability < boundaries[i + 1])
            )

        if not mask.any():
            continue

        confidence = float(
            probability[
                mask
            ].mean()
        )

        accuracy = float(
            y_true[
                mask
            ].mean()
        )

        weight = (
            mask.mean()
        )

        error += (
            weight
            * abs(
                accuracy
                - confidence
            )
        )

    return float(
        error
    )


def classifier_metrics(
    y_true,
    probability,
) -> dict:

    probability = np.clip(
        np.asarray(
            probability
        ),
        1e-7,
        1 - 1e-7,
    )

    y_true = np.asarray(
        y_true
    )

    prevalence = float(
        y_true.mean()
    )

    roc = float(
        roc_auc_score(
            y_true,
            probability,
        )
    )

    pr = float(
        average_precision_score(
            y_true,
            probability,
        )
    )

    return {
        "rows":
            int(
                len(y_true)
            ),

        "prevalence":
            prevalence,

        "roc_auc":
            roc,

        "pr_auc":
            pr,

        "pr_lift":
            (
                float(
                    pr / prevalence
                )
                if prevalence > 0
                else None
            ),

        "brier":
            float(
                brier_score_loss(
                    y_true,
                    probability,
                )
            ),

        "log_loss":
            float(
                log_loss(
                    y_true,
                    probability,
                    labels=[0, 1],
                )
            ),

        "ece_10":
            expected_calibration_error(
                y_true,
                probability,
                bins=10,
            ),
    }


# ============================================================
# CLASSIFICATION BACKTEST
# ============================================================

def evaluate_classifier_candidate(
    dev: pd.DataFrame,
    folds: list[dict],
    target: str,
    availability_column: str,
    features: list[str],
    model_factory: Callable[[], object],
) -> list[dict]:

    rows = []

    for fold in folds:

        eval_start = (
            pd.Timestamp(
                fold["eval_start"]
            )
            .tz_convert("UTC")
            if pd.Timestamp(
                fold["eval_start"]
            ).tzinfo
            else pd.Timestamp(
                fold["eval_start"],
                tz="UTC",
            )
        )

        eval_end = (
            pd.Timestamp(
                fold["eval_end"]
            )
            .tz_convert("UTC")
            if pd.Timestamp(
                fold["eval_end"]
            ).tzinfo
            else pd.Timestamp(
                fold["eval_end"],
                tz="UTC",
            )
        )

        # Historical invoices only.
        train = dev[
            dev[POSTING_DATE]
            < eval_start
        ].copy()

        # Critical purge:
        # outcome had to be knowable BEFORE
        # evaluation began.
        train = train[
            train[
                availability_column
            ]
            < eval_start
        ].copy()

        evaluation = dev[
            (
                dev[POSTING_DATE]
                >= eval_start
            )
            &
            (
                dev[POSTING_DATE]
                < (
                    eval_end
                    + pd.Timedelta(
                        days=1
                    )
                )
            )
        ].copy()

        y_train = (
            train[target]
            .astype(int)
        )

        y_eval = (
            evaluation[target]
            .astype(int)
        )

        if (
            len(train) < 1000
            or len(evaluation) < 100
            or y_train.nunique() < 2
            or y_eval.nunique() < 2
        ):
            continue

        model = make_pipeline(
            model_factory()
        )

        model.fit(
            train[features],
            y_train,
        )

        probability = (
            model.predict_proba(
                evaluation[
                    features
                ]
            )[:, 1]
        )

        metric = (
            classifier_metrics(
                y_eval,
                probability,
            )
        )

        metric.update(
            {
                "fold":
                    int(
                        fold["fold"]
                    ),

                "train_rows":
                    int(
                        len(train)
                    ),

                "train_positive_rate":
                    float(
                        y_train.mean()
                    ),

                "eval_start":
                    str(
                        eval_start
                    ),

                "eval_end":
                    str(
                        eval_end
                    ),
            }
        )

        rows.append(
            metric
        )

    return rows


def aggregate_classifier_folds(
    fold_metrics: list[dict],
) -> dict:

    if not fold_metrics:
        return {
            "valid":
                False,
        }

    aucs = np.array(
        [
            x["roc_auc"]
            for x
            in fold_metrics
        ]
    )

    prs = np.array(
        [
            x["pr_auc"]
            for x
            in fold_metrics
        ]
    )

    lifts = np.array(
        [
            x["pr_lift"]
            for x
            in fold_metrics
            if x["pr_lift"]
            is not None
        ]
    )

    briers = np.array(
        [
            x["brier"]
            for x
            in fold_metrics
        ]
    )

    eces = np.array(
        [
            x["ece_10"]
            for x
            in fold_metrics
        ]
    )

    return {
        "valid":
            True,

        "fold_count":
            int(
                len(
                    fold_metrics
                )
            ),

        "below_random_folds":
            int(
                (aucs < 0.50)
                .sum()
            ),

        "median_roc_auc":
            float(
                np.median(
                    aucs
                )
            ),

        "mean_roc_auc":
            float(
                aucs.mean()
            ),

        "worst_roc_auc":
            float(
                aucs.min()
            ),

        "roc_auc_std":
            float(
                aucs.std()
            ),

        "median_pr_auc":
            float(
                np.median(
                    prs
                )
            ),

        "median_pr_lift":
            (
                float(
                    np.median(
                        lifts
                    )
                )
                if len(lifts)
                else None
            ),

        "mean_brier":
            float(
                briers.mean()
            ),

        "mean_ece":
            float(
                eces.mean()
            ),
    }


def classifier_selection_key(
    aggregate: dict,
) -> tuple:

    """
    Stability-first selection.

    Priority:
    1. fewer below-random periods
    2. better worst temporal AUC
    3. better median temporal AUC
    4. lower AUC variance
    5. stronger PR lift
    6. lower Brier score
    """

    return (
        aggregate[
            "below_random_folds"
        ],

        -aggregate[
            "worst_roc_auc"
        ],

        -aggregate[
            "median_roc_auc"
        ],

        aggregate[
            "roc_auc_std"
        ],

        -(
            aggregate[
                "median_pr_lift"
            ]
            or 0
        ),

        aggregate[
            "mean_brier"
        ],

        aggregate[
            "mean_ece"
        ],
    )


# ============================================================
# REGRESSION MODELS
# ============================================================

def regression_factories() -> dict[
    str,
    Callable[[], object]
]:

    return {
        "random_forest":
            lambda: (
                RandomForestRegressor(
                    n_estimators=500,
                    max_depth=12,
                    min_samples_leaf=25,
                    max_features=0.5,
                    n_jobs=-1,
                    random_state=42,
                )
            ),

        "xgboost":
            lambda: (
                XGBRegressor(
                    n_estimators=500,
                    learning_rate=0.03,
                    max_depth=4,
                    min_child_weight=20,
                    subsample=0.85,
                    colsample_bytree=0.80,
                    reg_lambda=10.0,
                    objective="reg:squarederror",
                    eval_metric="mae",
                    random_state=42,
                    n_jobs=-1,
                )
            ),

        "gradient_boosting":
            lambda: (
                GradientBoostingRegressor(
                    n_estimators=400,
                    learning_rate=0.03,
                    max_depth=3,
                    min_samples_leaf=25,
                    subsample=0.85,
                    loss="huber",
                    random_state=42,
                )
            ),
    }


def regression_metrics(
    y_true,
    prediction,
) -> dict:

    prediction = np.maximum(
        np.asarray(
            prediction
        ),
        0,
    )

    return {
        "rows":
            int(
                len(y_true)
            ),

        "mae":
            float(
                mean_absolute_error(
                    y_true,
                    prediction,
                )
            ),

        "median_absolute_error":
            float(
                median_absolute_error(
                    y_true,
                    prediction,
                )
            ),

        "rmse":
            float(
                np.sqrt(
                    mean_squared_error(
                        y_true,
                        prediction,
                    )
                )
            ),

        "r2":
            float(
                r2_score(
                    y_true,
                    prediction,
                )
            ),

        "actual_mean":
            float(
                np.mean(
                    y_true
                )
            ),

        "actual_median":
            float(
                np.median(
                    y_true
                )
            ),
    }


def evaluate_regression_candidate(
    dev: pd.DataFrame,
    folds: list[dict],
    features: list[str],
    model_factory: Callable[[], object],
) -> list[dict]:

    rows = []

    for fold in folds:

        eval_start = pd.Timestamp(
            fold[
                "eval_start"
            ]
        )

        if eval_start.tzinfo is None:
            eval_start = eval_start.tz_localize(
                "UTC"
            )

        eval_end = pd.Timestamp(
            fold[
                "eval_end"
            ]
        )

        if eval_end.tzinfo is None:
            eval_end = eval_end.tz_localize(
                "UTC"
            )

        train = dev[
            dev[POSTING_DATE]
            < eval_start
        ].copy()

        # Exact days-late outcome known only
        # when invoice has cleared.
        train = train[
            train[
                "available_days_late"
            ]
            < eval_start
        ].copy()

        train = train[
            train[
                "target_late_payment"
            ] == 1
        ].copy()

        evaluation = dev[
            (
                dev[POSTING_DATE]
                >= eval_start
            )
            &
            (
                dev[POSTING_DATE]
                < (
                    eval_end
                    + pd.Timedelta(
                        days=1
                    )
                )
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

        y_train = train[
            "target_days_late_if_late"
        ].astype(float)

        y_eval = evaluation[
            "target_days_late_if_late"
        ].astype(float)

        base = make_pipeline(
            model_factory()
        )

        # Heavy right-tail:
        # train the expected-lateness model in log space.
        model = (
            TransformedTargetRegressor(
                regressor=base,
                func=np.log1p,
                inverse_func=np.expm1,
            )
        )

        model.fit(
            train[features],
            y_train,
        )

        prediction = model.predict(
            evaluation[
                features
            ]
        )

        metric = regression_metrics(
            y_eval,
            prediction,
        )

        metric.update(
            {
                "fold":
                    int(
                        fold["fold"]
                    ),

                "train_rows":
                    int(
                        len(train)
                    ),

                "eval_start":
                    str(
                        eval_start
                    ),

                "eval_end":
                    str(
                        eval_end
                    ),
            }
        )

        rows.append(
            metric
        )

    return rows


def aggregate_regression_folds(
    fold_metrics: list[dict],
) -> dict:

    if not fold_metrics:
        return {
            "valid":
                False,
        }

    maes = np.array(
        [
            x["mae"]
            for x
            in fold_metrics
        ]
    )

    medaes = np.array(
        [
            x[
                "median_absolute_error"
            ]
            for x
            in fold_metrics
        ]
    )

    return {
        "valid":
            True,

        "fold_count":
            int(
                len(
                    fold_metrics
                )
            ),

        "median_mae":
            float(
                np.median(
                    maes
                )
            ),

        "mean_mae":
            float(
                maes.mean()
            ),

        "worst_mae":
            float(
                maes.max()
            ),

        "mae_std":
            float(
                maes.std()
            ),

        "median_medae":
            float(
                np.median(
                    medaes
                )
            ),
    }


def regression_selection_key(
    aggregate: dict,
) -> tuple:

    return (
        aggregate[
            "median_mae"
        ],

        aggregate[
            "worst_mae"
        ],

        aggregate[
            "mae_std"
        ],

        aggregate[
            "median_medae"
        ],
    )


# ============================================================
# QUANTILE MODELS
# ============================================================

def evaluate_quantile_candidate(
    dev: pd.DataFrame,
    folds: list[dict],
    features: list[str],
    quantile: float,
) -> list[dict]:

    rows = []

    for fold in folds:

        eval_start = pd.Timestamp(
            fold[
                "eval_start"
            ]
        )

        if eval_start.tzinfo is None:
            eval_start = (
                eval_start
                .tz_localize(
                    "UTC"
                )
            )

        eval_end = pd.Timestamp(
            fold[
                "eval_end"
            ]
        )

        if eval_end.tzinfo is None:
            eval_end = (
                eval_end
                .tz_localize(
                    "UTC"
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
                ]
                == 1
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
                < (
                    eval_end
                    + pd.Timedelta(
                        days=1
                    )
                )
            )
            &
            (
                dev[
                    "target_late_payment"
                ]
                == 1
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
                alpha=quantile,
                n_estimators=450,
                learning_rate=0.03,
                max_depth=3,
                min_samples_leaf=25,
                subsample=0.85,
                random_state=42,
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
                evaluation[
                    features
                ]
            ),
            0,
        )

        loss = float(
            mean_pinball_loss(
                y_eval,
                prediction,
                alpha=quantile,
            )
        )

        coverage = float(
            (
                y_eval
                <= prediction
            ).mean()
        )

        rows.append(
            {
                "fold":
                    int(
                        fold[
                            "fold"
                        ]
                    ),

                "rows":
                    int(
                        len(
                            evaluation
                        )
                    ),

                "pinball_loss":
                    loss,

                "coverage":
                    coverage,

                "coverage_error":
                    abs(
                        coverage
                        - quantile
                    ),

                "eval_start":
                    str(
                        eval_start
                    ),

                "eval_end":
                    str(
                        eval_end
                    ),
            }
        )

    return rows


def aggregate_quantile_folds(
    rows: list[dict],
) -> dict:

    if not rows:
        return {
            "valid":
                False,
        }

    losses = np.array(
        [
            x[
                "pinball_loss"
            ]
            for x
            in rows
        ]
    )

    coverage_error = np.array(
        [
            x[
                "coverage_error"
            ]
            for x
            in rows
        ]
    )

    coverages = np.array(
        [
            x["coverage"]
            for x
            in rows
        ]
    )

    return {
        "valid":
            True,

        "fold_count":
            len(rows),

        "median_pinball":
            float(
                np.median(
                    losses
                )
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
                    coverage_error
                )
            ),
    }


# ============================================================
# FINAL FIT HELPERS
# ============================================================

def create_classifier(
    family: str,
):

    return classifier_factories()[
        family
    ]()


def create_regressor(
    family: str,
):

    return regression_factories()[
        family
    ]()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    ARTIFACT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    MODEL_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    REPORT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    banner(
        "RECOVERIQ — B2B V2 MULTI-TARGET TRAINING"
    )

    dev = load_development_data()

    dev = add_targets(
        dev
    )

    print(
        f"Development rows: "
        f"{len(dev):,}"
    )

    print(
        "Date range:",
        dev[
            POSTING_DATE
        ].min(),
        "→",
        dev[
            POSTING_DATE
        ].max(),
    )

    print(
        "\nTEST SPLIT WAS NOT READ."
    )

    # Validate feature contract.
    all_features = set()

    for features in (
        FEATURE_SETS.values()
    ):
        all_features.update(
            features
        )

    missing_features = (
        all_features
        - set(
            dev.columns
        )
    )

    if missing_features:
        raise ValueError(
            "Missing features: "
            f"{sorted(missing_features)}"
        )

    # --------------------------------------------------------
    # Folds
    # --------------------------------------------------------

    folds = build_temporal_folds(
        dev
    )

    banner(
        "TEMPORAL BACKTEST WINDOWS"
    )

    for fold in folds:

        print(
            f"fold {fold['fold']}: "
            f"train <= {fold['train_end'].date()}  |  "
            f"eval {fold['eval_start'].date()} "
            f"→ {fold['eval_end'].date()}"
        )

    # ========================================================
    # CLASSIFICATION HEADS
    # ========================================================

    classifier_report = {}

    selected_classifiers = {}

    for (
        head_name,
        head,
    ) in (
        CLASSIFICATION_TARGETS.items()
    ):

        banner(
            f"CLASSIFICATION HEAD — "
            f"{head_name.upper()}"
        )

        candidates = []

        for (
            feature_name,
            features,
        ) in (
            FEATURE_SETS.items()
        ):

            for (
                model_name,
                factory,
            ) in (
                classifier_factories()
                .items()
            ):

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
                        model_factory=factory,
                    )
                )

                aggregate = (
                    aggregate_classifier_folds(
                        rows
                    )
                )

                if not aggregate.get(
                    "valid"
                ):
                    continue

                candidate = {
                    "feature_set":
                        feature_name,

                    "model":
                        model_name,

                    "folds":
                        rows,

                    "aggregate":
                        aggregate,
                }

                candidates.append(
                    candidate
                )

                print(
                    f"{feature_name:<18}"
                    f"{model_name:<22}"
                    f"median_auc="
                    f"{aggregate['median_roc_auc']:.4f}  "
                    f"worst="
                    f"{aggregate['worst_roc_auc']:.4f}  "
                    f"std="
                    f"{aggregate['roc_auc_std']:.4f}  "
                    f"below50="
                    f"{aggregate['below_random_folds']}  "
                    f"PRlift="
                    f"{aggregate['median_pr_lift']:.2f}  "
                    f"Brier="
                    f"{aggregate['mean_brier']:.4f}"
                )

        if not candidates:
            raise RuntimeError(
                f"No valid candidates "
                f"for {head_name}"
            )

        winner = min(
            candidates,
            key=lambda x: (
                classifier_selection_key(
                    x[
                        "aggregate"
                    ]
                )
            ),
        )

        selected_classifiers[
            head_name
        ] = winner

        classifier_report[
            head_name
        ] = {
            "business_question":
                head[
                    "business_question"
                ],

            "target":
                head[
                    "target"
                ],

            "winner":
                winner,

            "candidates":
                candidates,
        }

        print()
        print(
            "SELECTED:",
            winner["model"],
            "|",
            winner["feature_set"],
        )

        print(
            "  median temporal AUC:",
            round(
                winner[
                    "aggregate"
                ][
                    "median_roc_auc"
                ],
                4,
            ),
        )

        print(
            "  worst temporal AUC: ",
            round(
                winner[
                    "aggregate"
                ][
                    "worst_roc_auc"
                ],
                4,
            ),
        )

    # ========================================================
    # CONDITIONAL MEAN LATENESS
    # ========================================================

    banner(
        "REGRESSION HEAD — "
        "CONDITIONAL DAYS LATE"
    )

    regression_candidates = []

    for (
        feature_name,
        features,
    ) in (
        FEATURE_SETS.items()
    ):

        for (
            model_name,
            factory,
        ) in (
            regression_factories()
            .items()
        ):

            rows = (
                evaluate_regression_candidate(
                    dev=dev,
                    folds=folds,
                    features=features,
                    model_factory=factory,
                )
            )

            aggregate = (
                aggregate_regression_folds(
                    rows
                )
            )

            if not aggregate.get(
                "valid"
            ):
                continue

            candidate = {
                "feature_set":
                    feature_name,

                "model":
                    model_name,

                "folds":
                    rows,

                "aggregate":
                    aggregate,
            }

            regression_candidates.append(
                candidate
            )

            print(
                f"{feature_name:<18}"
                f"{model_name:<22}"
                f"median_MAE="
                f"{aggregate['median_mae']:.3f}  "
                f"worst_MAE="
                f"{aggregate['worst_mae']:.3f}  "
                f"std="
                f"{aggregate['mae_std']:.3f}"
            )

    regression_winner = min(
        regression_candidates,
        key=lambda x: (
            regression_selection_key(
                x[
                    "aggregate"
                ]
            )
        ),
    )

    print()
    print(
        "SELECTED:",
        regression_winner[
            "model"
        ],
        "|",
        regression_winner[
            "feature_set"
        ],
    )

    # ========================================================
    # QUANTILES
    # ========================================================

    quantile_report = {}

    selected_quantiles = {}

    for quantile in [
        0.80,
    ]:

        label = (
            f"p{int(quantile * 100)}"
        )

        banner(
            f"QUANTILE LATENESS — "
            f"{label.upper()}"
        )

        candidates = []

        for (
            feature_name,
            features,
        ) in (
            FEATURE_SETS.items()
        ):

            rows = (
                evaluate_quantile_candidate(
                    dev=dev,
                    folds=folds,
                    features=features,
                    quantile=quantile,
                )
            )

            aggregate = (
                aggregate_quantile_folds(
                    rows
                )
            )

            if not aggregate.get(
                "valid"
            ):
                continue

            candidate = {
                "feature_set":
                    feature_name,

                "quantile":
                    quantile,

                "folds":
                    rows,

                "aggregate":
                    aggregate,
            }

            candidates.append(
                candidate
            )

            print(
                f"{feature_name:<20}"
                f"median_pinball="
                f"{aggregate['median_pinball']:.4f}  "
                f"worst="
                f"{aggregate['worst_pinball']:.4f}  "
                f"coverage="
                f"{aggregate['median_coverage']:.3f}  "
                f"coverage_error="
                f"{aggregate['median_coverage_error']:.3f}"
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

        selected_quantiles[
            label
        ] = winner

        quantile_report[
            label
        ] = {
            "winner":
                winner,

            "candidates":
                candidates,
        }

        print()
        print(
            "SELECTED:",
            winner[
                "feature_set"
            ],
        )

    # ========================================================
    # REFIT SELECTED MODELS
    # ========================================================

    banner(
        "REFIT SELECTED B2B V2 MODELS"
    )

    # Historical development cutoff.
    #
    # This keeps the refit consistent with an
    # as-of deployment immediately after validation.
    refit_as_of = (
        dev[POSTING_DATE]
        .max()
        .normalize()
        + pd.Timedelta(
            days=1
        )
    )

    final_training_manifest = {}

    # --------------------------------------------------------
    # Classification refit
    # --------------------------------------------------------

    for (
        head_name,
        winner,
    ) in (
        selected_classifiers.items()
    ):

        head = (
            CLASSIFICATION_TARGETS[
                head_name
            ]
        )

        features = (
            FEATURE_SETS[
                winner[
                    "feature_set"
                ]
            ]
        )

        final_train = dev[
            dev[
                head[
                    "availability"
                ]
            ]
            < refit_as_of
        ].copy()

        model = make_pipeline(
            create_classifier(
                winner[
                    "model"
                ]
            )
        )

        model.fit(
            final_train[
                features
            ],
            final_train[
                head[
                    "target"
                ]
            ].astype(int),
        )

        model_dir = (
            MODEL_ROOT
            / head_name
        )

        model_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        joblib.dump(
            model,
            model_dir
            / "model.joblib",
        )

        metadata = {
            "task":
                "classification",

            "head":
                head_name,

            "business_question":
                head[
                    "business_question"
                ],

            "model_family":
                winner[
                    "model"
                ],

            "feature_set":
                winner[
                    "feature_set"
                ],

            "features":
                features,

            "training_rows":
                int(
                    len(
                        final_train
                    )
                ),

            "positive_rate":
                float(
                    final_train[
                        head[
                            "target"
                        ]
                    ].mean()
                ),

            "refit_as_of":
                str(
                    refit_as_of
                ),

            "temporal_backtest":
                winner[
                    "aggregate"
                ],

            "test_used":
                False,
        }

        (
            model_dir
            / "metadata.json"
        ).write_text(
            json.dumps(
                metadata,
                indent=2,
            )
        )

        final_training_manifest[
            head_name
        ] = metadata

        print(
            f"{head_name:<20}"
            f"{winner['model']:<22}"
            f"{winner['feature_set']:<20}"
            f"rows={len(final_train):,}"
        )

    # --------------------------------------------------------
    # Mean lateness refit
    # --------------------------------------------------------

    mean_features = FEATURE_SETS[
        regression_winner[
            "feature_set"
        ]
    ]

    late_train = dev[
        (
            dev[
                "target_late_payment"
            ] == 1
        )
        &
        (
            dev[
                "available_days_late"
            ]
            < refit_as_of
        )
    ].copy()

    base = make_pipeline(
        create_regressor(
            regression_winner[
                "model"
            ]
        )
    )

    late_mean_model = (
        TransformedTargetRegressor(
            regressor=base,
            func=np.log1p,
            inverse_func=np.expm1,
        )
    )

    late_mean_model.fit(
        late_train[
            mean_features
        ],
        late_train[
            "target_days_late_if_late"
        ],
    )

    mean_dir = (
        MODEL_ROOT
        / "days_late_if_late_mean"
    )

    mean_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    joblib.dump(
        late_mean_model,
        mean_dir
        / "model.joblib",
    )

    mean_metadata = {
        "task":
            "conditional_regression",

        "business_question":
            (
                "If this invoice is late, "
                "how many days late is it "
                "expected to be?"
            ),

        "model_family":
            regression_winner[
                "model"
            ],

        "feature_set":
            regression_winner[
                "feature_set"
            ],

        "features":
            mean_features,

        "training_rows":
            int(
                len(
                    late_train
                )
            ),

        "temporal_backtest":
            regression_winner[
                "aggregate"
            ],

        "test_used":
            False,
    }

    (
        mean_dir
        / "metadata.json"
    ).write_text(
        json.dumps(
            mean_metadata,
            indent=2,
        )
    )

    final_training_manifest[
        "days_late_if_late_mean"
    ] = mean_metadata

    # --------------------------------------------------------
    # Quantile refit
    # --------------------------------------------------------

    for (
        label,
        winner,
    ) in (
        selected_quantiles.items()
    ):

        q = winner[
            "quantile"
        ]

        features = FEATURE_SETS[
            winner[
                "feature_set"
            ]
        ]

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
            late_train[
                features
            ],
            late_train[
                "target_days_late_if_late"
            ],
        )

        model_dir = (
            MODEL_ROOT
            / f"days_late_{label}"
        )

        model_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        joblib.dump(
            model,
            model_dir
            / "model.joblib",
        )

        metadata = {
            "task":
                "quantile_regression",

            "quantile":
                q,

            "business_question":
                (
                    f"What is the {label.upper()} "
                    f"lateness estimate if the "
                    f"invoice is late?"
                ),

            "feature_set":
                winner[
                    "feature_set"
                ],

            "features":
                features,

            "training_rows":
                int(
                    len(
                        late_train
                    )
                ),

            "temporal_backtest":
                winner[
                    "aggregate"
                ],

            "test_used":
                False,
        }

        (
            model_dir
            / "metadata.json"
        ).write_text(
            json.dumps(
                metadata,
                indent=2,
            )
        )

        final_training_manifest[
            f"days_late_{label}"
        ] = metadata

    # ========================================================
    # SAVE REPORTS
    # ========================================================

    banner(
        "SAVE B2B V2 REPORTS"
    )

    full_report = {
        "version":
            "b2b_v2",

        "development_rows":
            int(
                len(
                    dev
                )
            ),

        "development_start":
            str(
                dev[
                    POSTING_DATE
                ].min()
            ),

        "development_end":
            str(
                dev[
                    POSTING_DATE
                ].max()
            ),

        "test_split_read":
            False,

        "feature_sets":
            FEATURE_SETS,

        "temporal_folds": [
            {
                key:
                    (
                        str(value)
                        if isinstance(
                            value,
                            pd.Timestamp,
                        )
                        else value
                    )
                for key, value
                in fold.items()
            }
            for fold
            in folds
        ],

        "classification":
            classifier_report,

        "conditional_lateness":
            {
                "winner":
                    regression_winner,

                "candidates":
                    regression_candidates,
            },

        "quantiles":
            quantile_report,

        "final_models":
            final_training_manifest,
    }

    (
        REPORT_ROOT
        / "training_report.json"
    ).write_text(
        json.dumps(
            full_report,
            indent=2,
            default=str,
        )
    )

    serving_contract = {
        "version":
            "b2b_v2",

        "model_outputs": {
            "late_probability":
                (
                    "P(invoice clears after "
                    "adjusted due date)"
                ),

            "delay_over_7d_probability":
                (
                    "P(invoice becomes more "
                    "than 7 days late)"
                ),

            "delay_over_15d_probability":
                (
                    "P(invoice becomes more "
                    "than 15 days late)"
                ),

            "conditional_expected_days_late":
                (
                    "Expected days late "
                    "conditional on being late"
                ),

            "conservative_clearance_window_days":
                (
                    "Conservative clearance-window estimate "
                    "conditional on being late (P80 internally)"
                ),
        },

        "derived_outputs": {
            "expected_days_late":
                (
                    "late_probability * "
                    "conditional_expected_days_late"
                ),

            "expected_overdue_exposure":
                (
                    "invoice_amount * "
                    "late_probability"
                ),

            "severity":
                (
                    "derived from late_probability, "
                    "delay_over_7d_probability, "
                    "delay_over_15d_probability and "
                    "conditional lateness"
                ),

            "collection_priority":
                (
                    "policy-derived from exposure, "
                    "risk, severity and due-date urgency"
                ),

            "intervention_recommendation":
                (
                    "deterministic policy output; "
                    "not an independently trained target"
                ),
        },

        "important_constraints": [
            (
                "P(>15d) should never exceed "
                "P(>7d) at serving time; "
                "enforce monotonic post-processing."
            ),

            (
                "These models do not estimate "
                "causal intervention effectiveness."
            ),

            (
                "Reserved test split remains "
                "untouched during development."
            ),
        ],
    }

    (
        ARTIFACT_ROOT
        / "serving_contract.json"
    ).write_text(
        json.dumps(
            serving_contract,
            indent=2,
        )
    )

    print(
        "Training report:"
    )

    print(
        " ",
        REPORT_ROOT
        / "training_report.json"
    )

    print(
        "\nServing contract:"
    )

    print(
        " ",
        ARTIFACT_ROOT
        / "serving_contract.json"
    )

    # ========================================================
    # FINAL SUMMARY
    # ========================================================

    banner(
        "B2B V2 TRAINING COMPLETE"
    )

    print(
        "Selected classifier heads:"
    )

    for (
        head_name,
        winner,
    ) in (
        selected_classifiers.items()
    ):

        agg = winner[
            "aggregate"
        ]

        print(
            f"  {head_name:<18}"
            f"{winner['model']:<22}"
            f"{winner['feature_set']:<18}"
            f"median_auc="
            f"{agg['median_roc_auc']:.4f}  "
            f"worst_auc="
            f"{agg['worst_roc_auc']:.4f}"
        )

    print()
    print(
        "Conditional lateness:"
    )

    print(
        f"  mean: "
        f"{regression_winner['model']} / "
        f"{regression_winner['feature_set']}"
    )

    print(
        f"  conservative window (P80):  "
        f"{selected_quantiles['p80']['feature_set']}"
    )

    print()
    print(
        "TEST SPLIT WAS NEVER READ."
    )


if __name__ == "__main__":
    main()
