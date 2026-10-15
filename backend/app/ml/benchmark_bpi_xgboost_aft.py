"""XGBoost AFT challenger for BPI invoice time-to-clear."""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
import xgboost as xgb
from scipy.special import ndtr

from app.ml.benchmark_bpi_survival_models import (
    available_features,
    make_preprocessor,
)
from app.ml.train_bpi_survival import (
    ARTIFACT_ROOT,
    FOLDS,
    HORIZONS,
    aggregate_horizon_metrics,
    horizon_metrics,
    load_data,
    recensor_training_data,
    utc,
)


REPORT_PATH = (
    ARTIFACT_ROOT
    / "xgboost_aft_backtest.json"
)


def banner(title):
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def make_labels(frame):
    duration = np.maximum(
        frame["_fold_duration"]
        .astype(float)
        .to_numpy(),
        1e-3,
    )

    event = (
        frame["_fold_event"]
        .astype(bool)
        .to_numpy()
    )

    lower = duration.astype(
        np.float32
    )

    upper = np.where(
        event,
        duration,
        np.inf,
    ).astype(
        np.float32
    )

    return lower, upper


def make_dmatrix(
    X,
    lower=None,
    upper=None,
):
    dmatrix = xgb.DMatrix(X)

    if lower is not None:
        dmatrix.set_float_info(
            "label_lower_bound",
            lower,
        )

    if upper is not None:
        dmatrix.set_float_info(
            "label_upper_bound",
            upper,
        )

    return dmatrix


def predict_cdf(
    booster,
    dmatrix,
    scale,
):
    # XGBoost AFT normal prediction is on the
    # original survival-time scale.
    predicted_time = np.maximum(
        booster.predict(dmatrix),
        1e-6,
    )

    location = np.log(
        predicted_time
    )

    probabilities = {}

    for horizon in HORIZONS:
        z = (
            np.log(float(horizon))
            - location
        ) / scale

        probabilities[
            float(horizon)
        ] = np.clip(
            ndtr(z),
            0.0,
            1.0,
        )

    return pd.DataFrame(
        probabilities
    )


def print_metrics(metrics):
    for horizon in HORIZONS:
        item = metrics[
            str(horizon)
        ]

        auc = item[
            "roc_auc"
        ]

        auc_text = (
            "NA"
            if auc is None
            else f"{auc:.4f}"
        )

        print(
            f"{horizon:>2}d"
            f"  known={item['known_rows']:,}"
            f"  AUC={auc_text}"
            f"  Brier={item['brier']:.4f}"
            f"  ECE={item['ece']:.4f}"
        )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--max-train-cases",
        type=int,
        default=0,
        help="0 = full temporal training data",
    )

    parser.add_argument(
        "--rounds",
        type=int,
        default=200,
    )

    parser.add_argument(
        "--threads",
        type=int,
        default=4,
    )

    args = parser.parse_args()

    banner(
        "RECOVERIQ — BPI XGBOOST AFT"
    )

    df = load_data()

    numeric, categorical = (
        available_features(df)
    )

    features = (
        numeric
        + categorical
    )

    print(
        "Invoices:",
        f"{len(df):,}",
    )

    print(
        "Training cap:",
        (
            "FULL DATA"
            if args.max_train_cases <= 0
            else f"{args.max_train_cases:,}"
        ),
    )

    print(
        "Boosting rounds:",
        args.rounds,
    )

    print(
        "Threads:",
        args.threads,
    )

    scale = 1.20

    params = {
        "objective":
            "survival:aft",

        "eval_metric":
            "aft-nloglik",

        "tree_method":
            "hist",

        "learning_rate":
            0.05,

        "max_depth":
            4,

        "min_child_weight":
            20,

        "subsample":
            0.80,

        "colsample_bytree":
            0.80,

        "reg_lambda":
            5.0,

        "reg_alpha":
            0.10,

        "aft_loss_distribution":
            "normal",

        "aft_loss_distribution_scale":
            scale,

        "nthread":
            args.threads,

        "seed":
            42,

        "verbosity":
            0,
    }

    fold_reports = []

    for fold in FOLDS:

        banner(
            "xgboost_aft — "
            + fold["name"]
        )

        train = (
            recensor_training_data(
                df,
                fold["train_cutoff"],
            )
            .reset_index(drop=True)
        )

        if (
            args.max_train_cases > 0
            and len(train)
            > args.max_train_cases
        ):
            train = (
                train.tail(
                    args.max_train_cases
                )
                .reset_index(drop=True)
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
                    df["invoice_timestamp"]
                    >= eval_start
                )
                &
                (
                    df["invoice_timestamp"]
                    < eval_end
                )
            ]
            .copy()
            .reset_index(drop=True)
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
                scale=False,
            )
        )

        X_train = (
            preprocessor
            .fit_transform(
                train[features]
            )
        )

        X_eval = (
            preprocessor
            .transform(
                evaluation[features]
            )
        )

        print(
            "Encoded features:",
            X_train.shape[1],
        )

        lower, upper = (
            make_labels(train)
        )

        dtrain = make_dmatrix(
            X_train,
            lower,
            upper,
        )

        deval = make_dmatrix(
            X_eval
        )

        booster = xgb.train(
            params,
            dtrain,
            num_boost_round=args.rounds,
            evals=[
                (
                    dtrain,
                    "train",
                )
            ],
            verbose_eval=50,
        )

        cdf = predict_cdf(
            booster,
            deval,
            scale,
        )

        metrics = horizon_metrics(
            evaluation,
            cdf,
        )

        summary = (
            aggregate_horizon_metrics(
                metrics
            )
        )

        print_metrics(
            metrics
        )

        print(
            "Mean AUC:",
            f"{summary['mean_horizon_auc']:.4f}",
        )

        print(
            "Mean Brier:",
            f"{summary['mean_horizon_brier']:.4f}",
        )

        fold_reports.append(
            {
                "fold":
                    fold["name"],

                "training_invoices":
                    len(train),

                "evaluation_invoices":
                    len(evaluation),

                "metrics":
                    metrics,

                "summary":
                    summary,
            }
        )

    mean_aucs = [
        item["summary"][
            "mean_horizon_auc"
        ]
        for item in fold_reports
    ]

    mean_briers = [
        item["summary"][
            "mean_horizon_brier"
        ]
        for item in fold_reports
    ]

    worst_aucs = [
        item["summary"][
            "worst_horizon_auc"
        ]
        for item in fold_reports
    ]

    final_summary = {
        "median_mean_horizon_auc":
            float(
                np.median(
                    mean_aucs
                )
            ),

        "worst_horizon_auc_across_folds":
            float(
                np.min(
                    worst_aucs
                )
            ),

        "median_mean_horizon_brier":
            float(
                np.median(
                    mean_briers
                )
            ),
    }

    banner(
        "XGBOOST AFT SUMMARY"
    )

    print(
        "AUC:",
        f"{final_summary['median_mean_horizon_auc']:.4f}",
    )

    print(
        "WorstAUC:",
        f"{final_summary['worst_horizon_auc_across_folds']:.4f}",
    )

    print(
        "Brier:",
        f"{final_summary['median_mean_horizon_brier']:.4f}",
    )

    report = {
        "model":
            "xgboost_aft",

        "full_training":
            args.max_train_cases <= 0,

        "max_train_cases":
            args.max_train_cases,

        "rounds":
            args.rounds,

        "parameters":
            params,

        "folds":
            fold_reports,

        "summary":
            final_summary,

        "production_model_changed":
            False,
    }

    REPORT_PATH.write_text(
        json.dumps(
            report,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )

    print()
    print(
        "Production model changed: NO"
    )

    print(
        "Report:",
        REPORT_PATH,
    )


if __name__ == "__main__":
    main()
