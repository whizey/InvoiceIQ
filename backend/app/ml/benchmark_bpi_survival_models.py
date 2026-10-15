"""
RecoverIQ — BPI survival challenger benchmark.

Additional models:
    1. Cox proportional hazards
    2. Random Survival Forest
    3. Gradient Boosting Survival

These are benchmark models only.
They DO NOT overwrite the production LightGBM model.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from sksurv.ensemble import (
    GradientBoostingSurvivalAnalysis,
    RandomSurvivalForest,
)
from sksurv.linear_model import CoxPHSurvivalAnalysis, CoxnetSurvivalAnalysis
from sksurv.util import Surv

from app.ml.train_bpi_survival import (
    ARTIFACT_ROOT,
    CATEGORICAL_CANDIDATES,
    FOLDS,
    HORIZONS,
    NUMERIC_CANDIDATES,
    aggregate_horizon_metrics,
    horizon_metrics,
    load_data,
    recensor_training_data,
    selection_key,
    summarize_model,
    utc,
)


REPORT_PATH = (
    ARTIFACT_ROOT
    / "challenger_survival_backtest.json"
)

REFERENCE_REPORT_PATH = (
    ARTIFACT_ROOT
    / "temporal_backtest.json"
)

MODELS = (
    "cox_ph",
    "coxnet",
    "gradient_boosting_survival",
    "random_survival_forest",
)


def banner(title):
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def available_features(df):

    numeric = [
        column
        for column in NUMERIC_CANDIDATES
        if (
            column in df.columns
            and not column.startswith("interval_")
        )
    ]

    categorical = [
        column
        for column in CATEGORICAL_CANDIDATES
        if (
            column in df.columns
            and column != "hazard_interval"
        )
    ]

    return numeric, categorical


def make_preprocessor(
    numeric,
    categorical,
    scale=False,
):

    numeric_steps = [
        (
            "imputer",
            SimpleImputer(
                strategy="median"
            ),
        ),
    ]

    if scale:
        numeric_steps.append(
            (
                "scaler",
                StandardScaler(),
            )
        )

    numeric_pipe = Pipeline(
        numeric_steps
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
                    sparse_output=False,
                    dtype=np.float32,
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
        sparse_threshold=0.0,
    )


def make_model(name):

    if name == "cox_ph":

        return CoxPHSurvivalAnalysis(
            alpha=0.1,
            ties="efron",
            n_iter=200,
        )

    if name == "coxnet":

        return CoxnetSurvivalAnalysis(
            alphas=[0.01],
            l1_ratio=0.15,
            max_iter=20000,
            tol=1e-6,
            fit_baseline_model=True,
        )

    if name == "random_survival_forest":

        return RandomSurvivalForest(
            n_estimators=60,
            max_depth=10,
            min_samples_split=50,
            min_samples_leaf=25,
            max_features="sqrt",
            max_samples=0.50,
            n_jobs=1,
            random_state=42,
            low_memory=False,
        )

    if name == "gradient_boosting_survival":

        return GradientBoostingSurvivalAnalysis(
            loss="coxph",
            learning_rate=0.08,
            n_estimators=50,
            subsample=0.60,
            min_samples_split=80,
            min_samples_leaf=40,
            max_depth=1,
            max_features="sqrt",
            random_state=42,
            n_iter_no_change=6,
            validation_fraction=0.10,
            verbose=1,
        )

    raise ValueError(
        f"Unknown model: {name}"
    )


def make_target(df):

    duration = np.maximum(
        df["_fold_duration"]
        .astype(float)
        .to_numpy(),
        1e-3,
    )

    event = (
        df["_fold_event"]
        .astype(bool)
        .to_numpy()
    )

    return Surv.from_arrays(
        event=event,
        time=duration,
    )


def limit_training(
    df,
    max_train_cases,
):

    if (
        max_train_cases <= 0
        or len(df) <= max_train_cases
    ):
        return df

    return (
        df.sample(
            n=max_train_cases,
            random_state=42,
        )
        .sort_values(
            "invoice_timestamp"
        )
        .reset_index(
            drop=True
        )
    )


def predict_cdf(
    model,
    x,
):

    survival = (
        model.predict_survival_function(
            x,
            return_array=True,
        )
    )

    times = np.asarray(
        model.unique_times_,
        dtype=float,
    )

    predictions = {}

    for horizon in HORIZONS:

        index = (
            np.searchsorted(
                times,
                float(horizon),
                side="right",
            )
            - 1
        )

        if index < 0:

            probability = np.zeros(
                len(x)
            )

        else:

            index = min(
                index,
                survival.shape[1] - 1,
            )

            probability = (
                1.0
                -
                survival[:, index]
            )

        predictions[
            float(horizon)
        ] = np.clip(
            probability,
            0.0,
            1.0,
        )

    return pd.DataFrame(
        predictions
    )


def run_model(
    df,
    model_name,
    numeric,
    categorical,
    max_train_cases,
):

    reports = []

    for fold in FOLDS:

        banner(
            f"{model_name} — "
            f"{fold['name']}"
        )

        train = (
            recensor_training_data(
                df,
                fold["train_cutoff"],
            )
        )

        train = limit_training(
            train,
            max_train_cases,
        )

        eval_start = utc(
            fold["eval_start"]
        )

        eval_end = utc(
            fold["eval_end"]
        )

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
            "Training invoices:",
            f"{len(train):,}",
        )

        print(
            "Evaluation invoices:",
            f"{len(evaluation):,}",
        )

        preprocessor = (
            make_preprocessor(
                numeric,
                categorical,
                scale=(
                    model_name
                    == "cox_ph"
                ),
            )
        )

        x_train = (
            preprocessor
            .fit_transform(
                train
            )
        )

        x_eval = (
            preprocessor
            .transform(
                evaluation
            )
        )

        y_train = make_target(
            train
        )

        print(
            "Encoded features:",
            x_train.shape[1],
        )

        model = make_model(
            model_name
        )

        model.fit(
            x_train,
            y_train,
        )

        cdf = predict_cdf(
            model,
            x_eval,
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

            metric = metrics[
                str(horizon)
            ]

            auc = metric[
                "roc_auc"
            ]

            auc_text = (
                f"{auc:.4f}"
                if auc is not None
                else "NA"
            )

            print(
                f"{horizon:>2}d  "
                f"AUC={auc_text}  "
                f"Brier="
                f"{metric['brier']:.4f}  "
                f"ECE="
                f"{metric['ece']:.4f}"
            )

        print(
            "Mean AUC:",
            f"{aggregate['mean_horizon_auc']:.4f}",
        )

        print(
            "Mean Brier:",
            f"{aggregate['mean_horizon_brier']:.4f}",
        )

        reports.append(
            {
                "fold":
                    fold,

                "training_invoices":
                    len(train),

                "evaluation_invoices":
                    len(evaluation),

                "encoded_features":
                    x_train.shape[1],

                "horizons":
                    metrics,

                "aggregate":
                    aggregate,
            }
        )

    return reports


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--models",
        nargs="+",
        choices=MODELS,
        default=list(MODELS),
    )

    parser.add_argument(
        "--max-train-cases",
        type=int,
        default=0,
        help=(
            "0 = all historical rows. "
            "Use 50000 for a faster laptop run."
        ),
    )

    args = parser.parse_args()

    banner(
        "RECOVERIQ — BPI SURVIVAL CHALLENGERS"
    )

    df = load_data()

    numeric, categorical = (
        available_features(
            df
        )
    )

    print(
        "Invoices:",
        f"{len(df):,}",
    )

    print(
        "Models:",
        args.models,
    )

    print(
        "Training cap:",
        (
            "FULL DATA"
            if args.max_train_cases <= 0
            else f"{args.max_train_cases:,}"
        ),
    )

    report = {
        "task":
            "case_level_survival_challengers",

        "production_model_changed":
            False,

        "horizons":
            HORIZONS,

        "models":
            {},
    }

    for name in args.models:

        folds = run_model(
            df,
            name,
            numeric,
            categorical,
            args.max_train_cases,
        )

        report[
            "models"
        ][
            name
        ] = {
            "folds":
                folds,

            "summary":
                summarize_model(
                    folds
                ),
        }

    ranking = sorted(
        args.models,
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

    report[
        "challenger_ranking"
    ] = ranking

    report[
        "best_challenger"
    ] = ranking[0]

    if (
        REFERENCE_REPORT_PATH
        .exists()
    ):

        old = json.loads(
            REFERENCE_REPORT_PATH
            .read_text()
        )

        winner = old.get(
            "winner"
        )

        report[
            "current_production_reference"
        ] = {
            "winner":
                winner,

            "summary":
                old.get(
                    "models",
                    {},
                )
                .get(
                    winner,
                    {},
                )
                .get(
                    "summary"
                ),
        }

    REPORT_PATH.write_text(
        json.dumps(
            report,
            indent=2,
            default=str,
        )
        + "\n"
    )

    banner(
        "CHALLENGER RANKING"
    )

    for position, name in enumerate(
        ranking,
        start=1,
    ):

        summary = (
            report[
                "models"
            ][
                name
            ][
                "summary"
            ]
        )

        print(
            f"{position}. "
            f"{name:<30}"
            f"AUC="
            f"{summary['median_mean_horizon_auc']:.4f}  "
            f"WorstAUC="
            f"{summary['worst_horizon_auc_across_folds']:.4f}  "
            f"Brier="
            f"{summary['median_mean_horizon_brier']:.4f}"
        )

    print()
    print(
        "Best challenger:",
        ranking[0],
    )

    print(
        "Production model changed: NO"
    )

    print(
        "Report:",
        REPORT_PATH,
    )


if __name__ == "__main__":
    main()
