"""
RecoverIQ — BPI 2019 Discrete-Time Survival Model

Predicts invoice time-to-clear from the moment of Invoice Receipt.

Outputs:
    P(clear <= 7d)
    P(clear <= 15d)
    P(clear <= 30d)
    P(clear <= 45d)
    P(clear <= 60d)
    P(clear <= 90d)
    recovery curve
    median predicted clearance window

Models compared:
    1. Logistic discrete-time hazard baseline (SGD logistic)
    2. LightGBM discrete-time hazard model

CRITICAL:
Training cases are RE-CENSORED at each historical fold cutoff.
A historical fold cannot use a clearance event that happened later.

No B2B test data is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import OneHotEncoder
from sklearn.pipeline import Pipeline
from sklearn.linear_model import SGDClassifier

from sklearn.metrics import (
    roc_auc_score,
    average_precision_score,
    brier_score_loss,
)

from lightgbm import LGBMClassifier


# ============================================================
# PATHS
# ============================================================

DATA_PATH = Path(
    "../data/processed/bpi_2019/"
    "bpi_survival_features.csv.gz"
)

ARTIFACT_ROOT = Path(
    "app/ml/artifacts/bpi_survival"
)

MODEL_DIR = (
    ARTIFACT_ROOT
    / "models"
)

REPORT_PATH = (
    ARTIFACT_ROOT
    / "temporal_backtest.json"
)

CONTRACT_PATH = (
    ARTIFACT_ROOT
    / "serving_contract.json"
)


# ============================================================
# TIME GRID
# ============================================================

INTERVALS = [
    (0, 7),
    (7, 15),
    (15, 30),
    (30, 45),
    (45, 60),
    (60, 90),
    (90, 120),
    (120, 180),
    (180, 365),
]

# Backtest the full curve at each useful checkpoint.
HORIZONS = [
    7,
    15,
    30,
    45,
    60,
    90,
]

# Keep the default product response compact.
SERVING_HORIZONS = [
    7,
    30,
    60,
    90,
]


# ============================================================
# TEMPORAL BACKTEST
# ============================================================

FOLDS = [
    {
        "name": "fold_0",
        "train_cutoff": "2018-07-01",
        "eval_start": "2018-07-01",
        "eval_end": "2018-09-01",
    },
    {
        "name": "fold_1",
        "train_cutoff": "2018-09-01",
        "eval_start": "2018-09-01",
        "eval_end": "2018-11-01",
    },
    {
        "name": "fold_2",
        "train_cutoff": "2018-11-01",
        "eval_start": "2018-11-01",
        "eval_end": "2018-12-01",
    },
    {
        "name": "fold_3",
        "train_cutoff": "2018-12-01",
        "eval_start": "2018-12-01",
        "eval_end": "2019-01-18",
    },
]


# ============================================================
# FEATURES
# ============================================================

NUMERIC_CANDIDATES = [
    "invoice_amount_eur",

    "pre_invoice_event_count",
    "pre_goods_receipt_count",
    "pre_service_entry_count",
    "pre_purchase_requisition_count",
    "pre_order_confirmation_count",
    "pre_quantity_change_count",
    "pre_price_change_count",
    "pre_approval_change_count",
    "pre_delete_po_item_count",
    "pre_set_payment_block_count",
    "pre_remove_payment_block_count",

    "process_age_at_invoice_days",
    "pre_rework_count",

    "has_goods_receipt_before_invoice",
    "has_service_entry_before_invoice",
    "had_payment_block_activity_before_invoice",

    "vendor_prior_invoice_count",
    "vendor_prior_resolved_count",
    "vendor_prior_open_count",
    "vendor_historical_clear_rate",
    "vendor_historical_avg_clear_days",
    "vendor_recent5_avg_clear_days",
    "vendor_days_since_previous_invoice",
    "vendor_historical_avg_invoice_amount",
    "amount_vs_vendor_historical_avg",

    # Discrete-time hazard features
    "interval_index",
    "interval_start_days",
    "interval_end_days",
]

CATEGORICAL_CANDIDATES = [
    "company",
    "document_type",
    "item_category",
    "gr-based_inv_verif",
    "goods_receipt",

    # Flexible baseline hazard
    "hazard_interval",
]


# ============================================================
# UTILS
# ============================================================

def banner(title):

    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def utc(value):

    ts = pd.Timestamp(value)

    if ts.tzinfo is None:
        return ts.tz_localize("UTC")

    return ts.tz_convert("UTC")


def load_data():

    df = pd.read_csv(
        DATA_PATH,
        low_memory=False,
    )

    for column in [
        "invoice_timestamp",
        "clear_timestamp",
    ]:

        df[column] = pd.to_datetime(
            df[column],
            utc=True,
            errors="coerce",
        )

    df["event_observed"] = (
        df["event_observed"]
        .astype(int)
    )

    return df


def available_features(df):

    numeric = [
        c
        for c in NUMERIC_CANDIDATES
        if (
            c in df.columns
            or c.startswith("interval_")
        )
    ]

    categorical = [
        c
        for c in CATEGORICAL_CANDIDATES
        if (
            c in df.columns
            or c == "hazard_interval"
        )
    ]

    return numeric, categorical


# ============================================================
# TEMPORAL RE-CENSORING
# ============================================================

def recensor_training_data(
    df,
    cutoff,
):

    """
    Reconstruct exactly what would have been known at cutoff.

    Example:
        invoice = June 1
        actual clear = August 10
        fold cutoff = July 1

    Historical training view:
        duration = 30 days
        event_observed = 0

    We DO NOT reveal the August clearance.
    """

    cutoff = utc(
        cutoff
    )

    train = df[
        df["invoice_timestamp"]
        < cutoff
    ].copy()

    clear_known = (
        train[
            "clear_timestamp"
        ].notna()
        &
        (
            train[
                "clear_timestamp"
            ]
            < cutoff
        )
    )

    train[
        "_fold_event"
    ] = (
        clear_known
        .astype(int)
    )

    endpoint = (
        train[
            "clear_timestamp"
        ]
        .where(
            clear_known,
            cutoff,
        )
    )

    train[
        "_fold_duration"
    ] = (
        (
            endpoint
            -
            train[
                "invoice_timestamp"
            ]
        )
        .dt.total_seconds()
        /
        86400.0
    )

    train = train[
        train[
            "_fold_duration"
        ]
        >= 0
    ].copy()

    return train


# ============================================================
# PERSON-PERIOD / HAZARD EXPANSION
# ============================================================

def expand_hazard_rows(
    df,
    duration_column,
    event_column,
):

    """
    Convert one invoice into multiple interval-at-risk rows.

    Example:
        clears at day 37

        0-7      event=0
        7-15     event=0
        15-30    event=0
        30-45    event=1

    Example:
        censored at day 29

        0-7      event=0
        7-15     event=0

        15-30 is NOT included because its outcome
        is unknown at the end of that interval.
    """

    frames = []

    duration = (
        df[
            duration_column
        ].astype(float)
    )

    event = (
        df[
            event_column
        ].astype(int)
    )

    for (
        interval_index,
        (
            start,
            end,
        ),
    ) in enumerate(
        INTERVALS
    ):

        if start == 0:

            event_here = (
                (event == 1)
                &
                (duration >= 0)
                &
                (duration <= end)
            )

        else:

            event_here = (
                (event == 1)
                &
                (duration > start)
                &
                (duration <= end)
            )

        survived_interval = (
            duration >= end
        )

        include = (
            event_here
            |
            survived_interval
        )

        if not include.any():
            continue

        part = (
            df.loc[
                include
            ]
            .copy()
        )

        part[
            "interval_index"
        ] = interval_index

        part[
            "interval_start_days"
        ] = float(
            start
        )

        part[
            "interval_end_days"
        ] = float(
            end
        )

        part[
            "hazard_interval"
        ] = (
            f"{start}_{end}"
        )

        part[
            "hazard_event"
        ] = (
            event_here.loc[
                include
            ]
            .astype(int)
            .values
        )

        frames.append(
            part
        )

    if not frames:

        return pd.DataFrame()

    result = pd.concat(
        frames,
        ignore_index=True,
    )

    return result


# ============================================================
# PREPROCESSING
# ============================================================

def make_preprocessor(
    numeric,
    categorical,
):

    numeric_pipe = Pipeline(
        [
            (
                "imputer",
                SimpleImputer(
                    strategy="median"
                ),
            ),
        ]
    )

    categorical_pipe = Pipeline(
        [
            (
                "imputer",
                SimpleImputer(
                    strategy="most_frequent"
                ),
            ),

            (
                "onehot",
                OneHotEncoder(
                    handle_unknown="ignore",
                ),
            ),
        ]
    )

    return ColumnTransformer(
        [
            (
                "numeric",
                numeric_pipe,
                numeric,
            ),

            (
                "categorical",
                categorical_pipe,
                categorical,
            ),
        ],

        sparse_threshold=1.0,
    )


# ============================================================
# MODEL FACTORIES
# ============================================================

def make_logistic(
    numeric,
    categorical,
):

    return Pipeline(
        [
            (
                "preprocessor",
                make_preprocessor(
                    numeric,
                    categorical,
                ),
            ),

            (
                "model",
                SGDClassifier(
                    loss="log_loss",
                    penalty="l2",
                    alpha=1e-4,
                    max_iter=1500,
                    tol=1e-4,
                    random_state=42,
                ),
            ),
        ]
    )


def make_lightgbm(
    numeric,
    categorical,
):

    return Pipeline(
        [
            (
                "preprocessor",
                make_preprocessor(
                    numeric,
                    categorical,
                ),
            ),

            (
                "model",
                LGBMClassifier(
                    objective="binary",

                    n_estimators=650,
                    learning_rate=0.03,

                    num_leaves=31,
                    min_child_samples=100,

                    subsample=0.85,
                    subsample_freq=1,
                    colsample_bytree=0.80,

                    reg_alpha=1.0,
                    reg_lambda=10.0,

                    random_state=42,
                    n_jobs=-1,

                    verbosity=-1,
                ),
            ),
        ]
    )


# ============================================================
# RECOVERY CURVE PREDICTION
# ============================================================

def prediction_grid(
    cases,
):

    frames = []

    for (
        interval_index,
        (
            start,
            end,
        ),
    ) in enumerate(
        INTERVALS
    ):

        part = cases.copy()

        part[
            "_prediction_row"
        ] = np.arange(
            len(part)
        )

        part[
            "interval_index"
        ] = interval_index

        part[
            "interval_start_days"
        ] = float(
            start
        )

        part[
            "interval_end_days"
        ] = float(
            end
        )

        part[
            "hazard_interval"
        ] = (
            f"{start}_{end}"
        )

        frames.append(
            part
        )

    return pd.concat(
        frames,
        ignore_index=True,
    )


def predict_recovery_curves(
    model,
    cases,
):

    grid = prediction_grid(
        cases
    )

    probability = (
        model.predict_proba(
            grid
        )[:, 1]
    )

    probability = np.clip(
        probability,
        1e-6,
        1 - 1e-6,
    )

    grid[
        "_hazard"
    ] = probability

    hazard = (
        grid
        .pivot(
            index="_prediction_row",
            columns="interval_end_days",
            values="_hazard",
        )
        .sort_index(
            axis=1
        )
    )

    survival = (
        1.0
        -
        hazard
    ).cumprod(
        axis=1
    )

    cdf = (
        1.0
        -
        survival
    )

    return (
        hazard,
        survival,
        cdf,
    )


# ============================================================
# METRICS
# ============================================================

def expected_calibration_error(
    y,
    p,
    bins=10,
):

    y = np.asarray(
        y
    )

    p = np.asarray(
        p
    )

    edges = np.linspace(
        0,
        1,
        bins + 1,
    )

    result = 0.0

    for i in range(
        bins
    ):

        if i == bins - 1:

            mask = (
                (p >= edges[i])
                &
                (p <= edges[i + 1])
            )

        else:

            mask = (
                (p >= edges[i])
                &
                (p < edges[i + 1])
            )

        n = int(
            mask.sum()
        )

        if n == 0:
            continue

        result += (
            n
            / len(y)
        ) * abs(
            float(
                y[mask].mean()
            )
            -
            float(
                p[mask].mean()
            )
        )

    return float(
        result
    )


def horizon_metrics(
    cases,
    cdf,
):

    report = {}

    for horizon in HORIZONS:

        target = (
            f"clear_within_{horizon}d"
        )

        if target not in cases:
            continue

        known = (
            cases[
                target
            ].notna()
        )

        y = (
            cases.loc[
                known,
                target
            ]
            .astype(int)
            .to_numpy()
        )

        p = (
            cdf.loc[
                known.to_numpy(),
                float(
                    horizon
                ),
            ]
            .to_numpy()
        )

        metrics = {
            "known_rows":
                int(
                    len(y)
                ),

            "positive_rate":
                float(
                    y.mean()
                ),

            "brier":
                float(
                    brier_score_loss(
                        y,
                        p,
                    )
                ),

            "ece":
                expected_calibration_error(
                    y,
                    p,
                ),
        }

        if (
            len(
                np.unique(
                    y
                )
            )
            == 2
        ):

            metrics[
                "roc_auc"
            ] = float(
                roc_auc_score(
                    y,
                    p,
                )
            )

            metrics[
                "pr_auc"
            ] = float(
                average_precision_score(
                    y,
                    p,
                )
            )

        else:

            metrics[
                "roc_auc"
            ] = None

            metrics[
                "pr_auc"
            ] = None

        report[
            str(
                horizon
            )
        ] = metrics

    return report


def aggregate_horizon_metrics(
    metrics,
):

    brier = [
        m["brier"]
        for m in metrics.values()
    ]

    auc = [
        m["roc_auc"]
        for m in metrics.values()
        if m[
            "roc_auc"
        ] is not None
    ]

    ece = [
        m["ece"]
        for m in metrics.values()
    ]

    return {
        # This is mean Brier across the fixed horizons,
        # NOT yet a formal IPCW Integrated Brier Score.
        "mean_horizon_brier":
            float(
                np.mean(
                    brier
                )
            ),

        "mean_horizon_auc":
            float(
                np.mean(
                    auc
                )
            ),

        "worst_horizon_auc":
            float(
                np.min(
                    auc
                )
            ),

        "mean_horizon_ece":
            float(
                np.mean(
                    ece
                )
            ),
    }


# ============================================================
# BACKTEST ONE MODEL
# ============================================================

def temporal_backtest(
    df,
    model_name,
    factory,
    numeric,
    categorical,
):

    fold_reports = []

    for fold in FOLDS:

        banner(
            f"{model_name.upper()} — {fold['name']}"
        )

        cutoff = utc(
            fold[
                "train_cutoff"
            ]
        )

        eval_start = utc(
            fold[
                "eval_start"
            ]
        )

        eval_end = utc(
            fold[
                "eval_end"
            ]
        )

        # --------------------------------------------
        # Historical training view
        # --------------------------------------------

        train_cases = (
            recensor_training_data(
                df,
                cutoff,
            )
        )

        train_hazard = (
            expand_hazard_rows(
                train_cases,
                "_fold_duration",
                "_fold_event",
            )
        )

        # --------------------------------------------
        # Evaluation invoices
        # --------------------------------------------

        evaluation = (
            df[
                (
                    df[
                        "invoice_timestamp"
                    ]
                    >= eval_start
                )
                &
                (
                    df[
                        "invoice_timestamp"
                    ]
                    < eval_end
                )
            ]
            .copy()
            .reset_index(
                drop=True
            )
        )

        print(
            f"Training invoices: "
            f"{len(train_cases):,}"
        )

        print(
            f"Hazard rows: "
            f"{len(train_hazard):,}"
        )

        print(
            f"Evaluation invoices: "
            f"{len(evaluation):,}"
        )

        model = factory(
            numeric,
            categorical,
        )

        model.fit(
            train_hazard,
            train_hazard[
                "hazard_event"
            ].astype(int),
        )

        (
            _,
            _,
            cdf,
        ) = (
            predict_recovery_curves(
                model,
                evaluation,
            )
        )

        metrics = horizon_metrics(
            evaluation,
            cdf,
        )

        aggregate = (
            aggregate_horizon_metrics(
                metrics
            )
        )

        for horizon in HORIZONS:

            m = metrics[
                str(
                    horizon
                )
            ]

            auc_text = (
                f"{m['roc_auc']:.4f}"
                if m[
                    "roc_auc"
                ] is not None
                else "NA"
            )

            print(
                f"{horizon:>2}d  "
                f"known={m['known_rows']:>6,}  "
                f"AUC={auc_text}  "
                f"Brier={m['brier']:.4f}  "
                f"ECE={m['ece']:.4f}"
            )

        print(
            f"Mean horizon Brier: "
            f"{aggregate['mean_horizon_brier']:.4f}"
        )

        print(
            f"Mean horizon AUC: "
            f"{aggregate['mean_horizon_auc']:.4f}"
        )

        fold_reports.append(
            {
                "fold":
                    fold,

                "training_invoices":
                    int(
                        len(
                            train_cases
                        )
                    ),

                "hazard_rows":
                    int(
                        len(
                            train_hazard
                        )
                    ),

                "evaluation_invoices":
                    int(
                        len(
                            evaluation
                        )
                    ),

                "horizons":
                    metrics,

                "aggregate":
                    aggregate,
            }
        )

    return fold_reports


# ============================================================
# MODEL COMPARISON
# ============================================================

def summarize_model(
    folds,
):

    brier = np.array(
        [
            f[
                "aggregate"
            ][
                "mean_horizon_brier"
            ]
            for f in folds
        ]
    )

    auc = np.array(
        [
            f[
                "aggregate"
            ][
                "mean_horizon_auc"
            ]
            for f in folds
        ]
    )

    worst_auc = np.array(
        [
            f[
                "aggregate"
            ][
                "worst_horizon_auc"
            ]
            for f in folds
        ]
    )

    ece = np.array(
        [
            f[
                "aggregate"
            ][
                "mean_horizon_ece"
            ]
            for f in folds
        ]
    )

    return {
        "median_mean_horizon_brier":
            float(
                np.median(
                    brier
                )
            ),

        "worst_mean_horizon_brier":
            float(
                brier.max()
            ),

        "median_mean_horizon_auc":
            float(
                np.median(
                    auc
                )
            ),

        "worst_horizon_auc_across_folds":
            float(
                worst_auc.min()
            ),

        "auc_std":
            float(
                auc.std()
            ),

        "median_mean_horizon_ece":
            float(
                np.median(
                    ece
                )
            ),
    }


def selection_key(
    summary,
):

    # Prefer reliable horizon discrimination first,
    # then Brier/calibration.
    return (
        -summary[
            "worst_horizon_auc_across_folds"
        ],

        -summary[
            "median_mean_horizon_auc"
        ],

        summary[
            "auc_std"
        ],

        summary[
            "median_mean_horizon_brier"
        ],

        summary[
            "median_mean_horizon_ece"
        ],
    )


# ============================================================
# FINAL REFIT
# ============================================================

def final_refit(
    df,
    factory,
    model_name,
    numeric,
    categorical,
):

    observation_cutoff = (
        max(
            df[
                "invoice_timestamp"
            ].max(),
            df[
                "clear_timestamp"
            ].max(),
        )
    )

    # Use the actual maximum censoring endpoint.
    endpoint = (
        df[
            "invoice_timestamp"
        ]
        +
        pd.to_timedelta(
            df[
                "duration_days"
            ],
            unit="D",
        )
    )

    observation_cutoff = (
        endpoint.max()
    )

    final_cases = (
        recensor_training_data(
            df,
            observation_cutoff
            + pd.Timedelta(
                seconds=1
            ),
        )
    )

    hazard = expand_hazard_rows(
        final_cases,
        "_fold_duration",
        "_fold_event",
    )

    print()
    print(
        f"Final training invoices: "
        f"{len(final_cases):,}"
    )

    print(
        f"Final hazard rows: "
        f"{len(hazard):,}"
    )

    model = factory(
        numeric,
        categorical,
    )

    model.fit(
        hazard,
        hazard[
            "hazard_event"
        ].astype(int),
    )

    MODEL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = (
        MODEL_DIR
        / "hazard_model.joblib"
    )

    joblib.dump(
        model,
        path,
    )

    return (
        path,
        len(final_cases),
        len(hazard),
    )


# ============================================================
# MAIN
# ============================================================

def main():

    ARTIFACT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    banner(
        "RECOVERIQ — BPI DISCRETE-TIME SURVIVAL"
    )

    df = load_data()

    numeric, categorical = (
        available_features(
            df
        )
    )

    print(
        f"Invoices: {len(df):,}"
    )

    print(
        f"Observed clears: "
        f"{df['event_observed'].sum():,}"
    )

    print(
        f"Censored: "
        f"{(df['event_observed'] == 0).sum():,}"
    )

    print()
    print(
        f"Numeric features: "
        f"{len(numeric)}"
    )

    print(
        f"Categorical features: "
        f"{categorical}"
    )

    candidates = {
        "logistic_hazard":
            make_logistic,

        "lightgbm_hazard":
            make_lightgbm,
    }

    report = {
        "task":
            "discrete_time_survival",

        "intervals":
            INTERVALS,

        "horizons":
            HORIZONS,

        "models": {},
    }

    for (
        model_name,
        factory,
    ) in candidates.items():

        banner(
            f"TEMPORAL BACKTEST — {model_name}"
        )

        folds = temporal_backtest(
            df,
            model_name,
            factory,
            numeric,
            categorical,
        )

        summary = summarize_model(
            folds
        )

        report[
            "models"
        ][
            model_name
        ] = {
            "folds":
                folds,

            "summary":
                summary,
        }

        print()
        print(
            f"{model_name} SUMMARY"
        )

        print(
            json.dumps(
                summary,
                indent=2,
            )
        )

    ranked = sorted(
        candidates.keys(),
        key=lambda name:
            selection_key(
                report[
                    "models"
                ][
                    name
                ][
                    "summary"
                ]
            ),
    )

    winner = ranked[0]

    report[
        "ranking"
    ] = ranked

    report[
        "winner"
    ] = winner

    banner(
        "MODEL SELECTION"
    )

    for i, name in enumerate(
        ranked,
        start=1,
    ):

        s = report[
            "models"
        ][
            name
        ][
            "summary"
        ]

        print(
            f"{i}. {name:<20}"
            f"medianAUC="
            f"{s['median_mean_horizon_auc']:.4f}  "
            f"worstAUC="
            f"{s['worst_horizon_auc_across_folds']:.4f}  "
            f"Brier="
            f"{s['median_mean_horizon_brier']:.4f}"
        )

    print()
    print(
        "WINNER:",
        winner
    )

    model_path, n_cases, n_hazard = (
        final_refit(
            df,
            candidates[
                winner
            ],
            winner,
            numeric,
            categorical,
        )
    )

    report[
        "final_model"
    ] = {
        "model":
            winner,

        "path":
            str(
                model_path
            ),

        "training_invoices":
            int(
                n_cases
            ),

        "training_hazard_rows":
            int(
                n_hazard
            ),
    }

    REPORT_PATH.write_text(
        json.dumps(
            report,
            indent=2,
            default=str,
        )
    )

    contract = {
        "engine":
            "BPI discrete-time survival",

        "prediction_timestamp":
            "Record Invoice Receipt",

        "default_outputs": [
            "clearance_probability_7d",
            "clearance_probability_30d",
            "clearance_probability_60d",
            "clearance_probability_90d",
            "likely_clearance_window_days",
        ],

        "advanced_outputs": [
            "recovery_curve",
        ],

        "serving_horizons_days":
            SERVING_HORIZONS,

        "evaluation_horizons_days":
            HORIZONS,

        "max_model_horizon_days":
            365,

        "censoring":
            "right-censoring supported",

        "training_leakage_control":
            (
                "Historical training invoices are re-censored "
                "at every temporal fold cutoff."
            ),

        "direct_vendor_id_used":
            False,

        "formal_ipcw_integrated_brier_score":
            False,

        "likely_clearance_window_definition":
            (
                "Earliest modeled time window where cumulative "
                "clearance probability reaches at least 50%."
            ),

        "note":
            (
                "Current Brier metrics are horizon-specific "
                "known-outcome metrics. Formal IPCW survival "
                "calibration is not claimed."
            ),
    }

    CONTRACT_PATH.write_text(
        json.dumps(
            contract,
            indent=2,
        )
    )

    banner(
        "BPI SURVIVAL TRAINING COMPLETE"
    )

    print(
        "Winner:",
        winner
    )

    print(
        "Model:"
    )

    print(
        " ",
        model_path.resolve()
    )

    print(
        "Report:"
    )

    print(
        " ",
        REPORT_PATH.resolve()
    )

    print(
        "Serving contract:"
    )

    print(
        " ",
        CONTRACT_PATH.resolve()
    )


if __name__ == "__main__":
    main()
