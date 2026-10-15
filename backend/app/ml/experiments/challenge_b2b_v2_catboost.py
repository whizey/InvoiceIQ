"""
RecoverIQ — Full Categorical Model Tournament

Models:
- Random Forest
- Gradient Boosting
- XGBoost
- LightGBM
- CatBoost

All models receive the SAME information:
    numeric engineered features
    + payment_method_description
    + region
    + city

RF / GB / XGB / LightGBM:
    categorical features -> one-hot encoding

CatBoost:
    categorical features -> native CatBoost handling

Evaluation:
- same temporal folds
- same label-availability purge
- same targets
- same stability-first selection

IMPORTANT:
Reserved test split is NEVER read.
No production model is overwritten.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import OneHotEncoder

from sklearn.ensemble import (
    RandomForestClassifier,
    GradientBoostingClassifier,
    RandomForestRegressor,
    GradientBoostingRegressor,
)

from sklearn.metrics import (
    roc_auc_score,
    average_precision_score,
    brier_score_loss,
    mean_absolute_error,
    mean_squared_error,
    r2_score,
)

from xgboost import (
    XGBClassifier,
    XGBRegressor,
)

from lightgbm import (
    LGBMClassifier,
    LGBMRegressor,
)

from catboost import (
    CatBoostClassifier,
    CatBoostRegressor,
)

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    FEATURE_SETS,
    CLASSIFICATION_TARGETS,
    POSTING_DATE,
    add_targets,
    build_temporal_folds,
    load_development_data,
)


# ============================================================
# CONFIG
# ============================================================

RAW_PATH = Path(
    "../data/raw/b2b_primary/"
    "b2b_invoice_payment_45839.csv"
)

OUTPUT_DIR = (
    ARTIFACT_ROOT
    / "categorical_model_tournament"
)

REPORT_PATH = (
    OUTPUT_DIR
    / "report.json"
)

CATEGORICAL_FEATURES = [
    "payment_method_description",
]

# We deliberately DO NOT use:
#
# cust_num
#     Customer identity can encourage memorization and
#     weaken cold-start generalization.
#
# zipcode
#     Higher-cardinality geographic identifier; leave it
#     out of the first clean categorical experiment.


# ============================================================
# UTILS
# ============================================================

def banner(title):

    print()
    print("=" * 94)
    print(title)
    print("=" * 94)


def utc(value):

    ts = pd.Timestamp(value)

    if ts.tzinfo is None:
        return ts.tz_localize("UTC")

    return ts.tz_convert("UTC")


def normalize_document_no(series):

    return (
        series
        .astype(str)
        .str.strip()
        .str.replace(
            r"\.0$",
            "",
            regex=True,
        )
    )


# ============================================================
# ADD SAFE RAW CATEGORIES
# ============================================================

def add_categorical_features(dev):

    if not RAW_PATH.exists():
        raise FileNotFoundError(
            f"Raw dataset not found: {RAW_PATH}"
        )

    # Raw source uses title/capitalized column names.
    # Rename them into the canonical names used by the
    # RecoverIQ ML pipeline.
    raw = pd.read_csv(
        RAW_PATH,
        usecols=[
            "Document_No",
            "Payment_Method_description",
        ],
        low_memory=False,
    ).rename(
        columns={
            "Document_No":
                "document_no",

            "Payment_Method_description":
                "payment_method_description",
        }
    )

    raw["document_no"] = (
        normalize_document_no(
            raw["document_no"]
        )
    )

    dev = dev.copy()

    dev["document_no"] = (
        normalize_document_no(
            dev["document_no"]
        )
    )

    # Verify that a document does not map to conflicting
    # categorical attributes.
    grouped = raw.groupby(
        "document_no",
        dropna=False,
    )

    for column in CATEGORICAL_FEATURES:

        conflicts = (
            grouped[column]
            .nunique(
                dropna=True
            )
        )

        conflict_count = int(
            (
                conflicts > 1
            ).sum()
        )

        if conflict_count:
            raise RuntimeError(
                f"{column}: "
                f"{conflict_count} document(s) "
                "have conflicting values."
            )

    raw = (
        raw
        .drop_duplicates(
            subset=[
                "document_no"
            ]
        )
    )

    before = len(dev)

    dev = dev.merge(
        raw,
        on="document_no",
        how="left",
        validate="many_to_one",
    )

    if len(dev) != before:
        raise RuntimeError(
            "Categorical merge changed row count."
        )

    for column in CATEGORICAL_FEATURES:

        dev[column] = (
            dev[column]
            .fillna("__MISSING__")
            .astype(str)
        )

    print()
    print("Categorical features:")

    for column in CATEGORICAL_FEATURES:

        print(
            f"  {column:<30}"
            f"{dev[column].nunique():>6} categories"
        )

    missing = dev[
        CATEGORICAL_FEATURES
    ].isna().sum().sum()

    if missing:
        raise RuntimeError(
            "Unexpected categorical NaNs remain."
        )

    return dev


# ============================================================
# PREPROCESSOR FOR NON-CATBOOST MODELS
# ============================================================

def make_preprocessor(
    numeric_features,
):

    numeric = Pipeline(
        steps=[
            (
                "imputer",
                SimpleImputer(
                    strategy="median"
                ),
            ),
        ]
    )

    categorical = Pipeline(
        steps=[
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
                ),
            ),
        ]
    )

    return ColumnTransformer(
        transformers=[
            (
                "numeric",
                numeric,
                numeric_features,
            ),

            (
                "categorical",
                categorical,
                CATEGORICAL_FEATURES,
            ),
        ],

        remainder="drop",
    )


# ============================================================
# MODEL FACTORIES
# ============================================================

def classifier_factory(
    name,
    numeric_features,
):

    if name == "random_forest":

        estimator = RandomForestClassifier(
            n_estimators=600,
            max_depth=12,
            min_samples_leaf=40,
            max_features=0.5,
            random_state=42,
            n_jobs=-1,
        )

    elif name == "gradient_boosting":

        estimator = GradientBoostingClassifier(
            n_estimators=350,
            learning_rate=0.035,
            max_depth=3,
            min_samples_leaf=40,
            random_state=42,
        )

    elif name == "xgboost":

        estimator = XGBClassifier(
            objective="binary:logistic",
            eval_metric="logloss",
            n_estimators=700,
            learning_rate=0.025,
            max_depth=4,
            min_child_weight=10,
            subsample=0.85,
            colsample_bytree=0.80,
            reg_alpha=1.0,
            reg_lambda=10.0,
            random_state=42,
            n_jobs=-1,
        )

    elif name == "lightgbm":

        estimator = LGBMClassifier(
            objective="binary",
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

    else:
        raise ValueError(
            name
        )

    return Pipeline(
        steps=[
            (
                "preprocessor",
                make_preprocessor(
                    numeric_features
                ),
            ),

            (
                "model",
                estimator,
            ),
        ]
    )


def catboost_classifier():

    return CatBoostClassifier(
        loss_function="Logloss",
        eval_metric="AUC",

        iterations=700,
        learning_rate=0.025,
        depth=6,

        l2_leaf_reg=10.0,

        random_seed=42,

        verbose=False,
        allow_writing_files=False,

        thread_count=-1,
    )


# ============================================================
# REGRESSION
# ============================================================

def regressor_factory(
    name,
    numeric_features,
):

    if name == "random_forest":

        estimator = RandomForestRegressor(
            n_estimators=600,
            max_depth=12,
            min_samples_leaf=40,
            max_features=0.5,
            random_state=42,
            n_jobs=-1,
        )

    elif name == "gradient_boosting":

        estimator = GradientBoostingRegressor(
            loss="huber",
            n_estimators=350,
            learning_rate=0.035,
            max_depth=3,
            min_samples_leaf=40,
            random_state=42,
        )

    elif name == "xgboost":

        estimator = XGBRegressor(
            objective="reg:absoluteerror",

            n_estimators=700,
            learning_rate=0.025,

            max_depth=4,
            min_child_weight=10,

            subsample=0.85,
            colsample_bytree=0.80,

            reg_alpha=1.0,
            reg_lambda=10.0,

            random_state=42,
            n_jobs=-1,
        )

    elif name == "lightgbm":

        estimator = LGBMRegressor(
            objective="regression_l1",

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

    else:
        raise ValueError(
            name
        )

    return Pipeline(
        steps=[
            (
                "preprocessor",
                make_preprocessor(
                    numeric_features
                ),
            ),

            (
                "model",
                estimator,
            ),
        ]
    )


def catboost_regressor():

    return CatBoostRegressor(
        loss_function="MAE",

        iterations=700,
        learning_rate=0.025,
        depth=6,

        l2_leaf_reg=10.0,

        random_seed=42,

        verbose=False,
        allow_writing_files=False,

        thread_count=-1,
    )


# ============================================================
# METRICS
# ============================================================

def ece(
    y,
    probability,
    bins=10,
):

    y = np.asarray(y)
    probability = np.asarray(
        probability
    )

    edges = np.linspace(
        0,
        1,
        bins + 1,
    )

    total = len(y)

    result = 0.0

    for i in range(bins):

        if i == bins - 1:
            mask = (
                (probability >= edges[i])
                &
                (probability <= edges[i + 1])
            )

        else:
            mask = (
                (probability >= edges[i])
                &
                (probability < edges[i + 1])
            )

        count = int(
            mask.sum()
        )

        if count == 0:
            continue

        observed = float(
            y[mask].mean()
        )

        predicted = float(
            probability[mask].mean()
        )

        result += (
            count / total
        ) * abs(
            observed
            - predicted
        )

    return float(result)


def classification_metrics(
    y,
    probability,
):

    y = np.asarray(y)

    probability = np.asarray(
        probability
    )

    prevalence = float(
        y.mean()
    )

    pr = float(
        average_precision_score(
            y,
            probability,
        )
    )

    return {
        "roc_auc":
            float(
                roc_auc_score(
                    y,
                    probability,
                )
            ),

        "pr_auc":
            pr,

        "pr_lift":
            float(
                pr / prevalence
            )
            if prevalence > 0
            else None,

        "brier":
            float(
                brier_score_loss(
                    y,
                    probability,
                )
            ),

        "ece":
            ece(
                y,
                probability,
            ),

        "positive_rate":
            prevalence,

        "rows":
            int(len(y)),
    }


# ============================================================
# CATBOOST DATA PREPARATION
# ============================================================

def catboost_frame(
    frame,
    numeric_features,
):

    columns = (
        list(
            numeric_features
        )
        + CATEGORICAL_FEATURES
    )

    result = frame[
        columns
    ].copy()

    for column in CATEGORICAL_FEATURES:

        result[column] = (
            result[column]
            .fillna("__MISSING__")
            .astype(str)
        )

    return result


# ============================================================
# CLASSIFICATION FOLD EVALUATION
# ============================================================

def evaluate_classifier(
    dev,
    folds,
    head_name,
    numeric_features,
    model_name,
):

    head = (
        CLASSIFICATION_TARGETS[
            head_name
        ]
    )

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
                    head[
                        "availability"
                    ]
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

        y_train = (
            train[
                head["target"]
            ]
            .astype(int)
        )

        y_eval = (
            evaluation[
                head["target"]
            ]
            .astype(int)
        )

        if (
            len(train) < 1000
            or len(evaluation) < 100
            or y_train.nunique() < 2
            or y_eval.nunique() < 2
        ):
            continue

        if model_name == "catboost":

            model = (
                catboost_classifier()
            )

            X_train = catboost_frame(
                train,
                numeric_features,
            )

            X_eval = catboost_frame(
                evaluation,
                numeric_features,
            )

            model.fit(
                X_train,
                y_train,
                cat_features=(
                    CATEGORICAL_FEATURES
                ),
            )

        else:

            model = classifier_factory(
                model_name,
                numeric_features,
            )

            X_train = train
            X_eval = evaluation

            model.fit(
                X_train,
                y_train,
            )

        probability = (
            model.predict_proba(
                X_eval
            )[:, 1]
        )

        metrics = (
            classification_metrics(
                y_eval,
                probability,
            )
        )

        metrics["fold"] = int(
            fold["fold"]
        )

        rows.append(
            metrics
        )

    return rows


def aggregate_classifier(
    rows,
):

    auc = np.array(
        [
            row["roc_auc"]
            for row in rows
        ]
    )

    pr_lift = np.array(
        [
            row["pr_lift"]
            for row in rows
            if row["pr_lift"]
            is not None
        ]
    )

    brier = np.array(
        [
            row["brier"]
            for row in rows
        ]
    )

    eces = np.array(
        [
            row["ece"]
            for row in rows
        ]
    )

    return {
        "fold_count":
            int(len(rows)),

        "below_random_folds":
            int(
                (
                    auc < 0.50
                ).sum()
            ),

        "median_roc_auc":
            float(
                np.median(auc)
            ),

        "worst_roc_auc":
            float(
                auc.min()
            ),

        "roc_auc_std":
            float(
                auc.std()
            ),

        "median_pr_lift":
            float(
                np.median(
                    pr_lift
                )
            ),

        "median_brier":
            float(
                np.median(
                    brier
                )
            ),

        "median_ece":
            float(
                np.median(
                    eces
                )
            ),
    }


def classifier_key(
    aggregate,
):

    # Stability first.
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

        -aggregate[
            "median_pr_lift"
        ],

        aggregate[
            "median_brier"
        ],
    )


# ============================================================
# REGRESSION EVALUATION
# ============================================================

def evaluate_regressor(
    dev,
    folds,
    numeric_features,
    model_name,
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

        y_train = (
            train[
                "target_days_late_if_late"
            ]
            .astype(float)
        )

        y_eval = (
            evaluation[
                "target_days_late_if_late"
            ]
            .astype(float)
        )

        if model_name == "catboost":

            model = (
                catboost_regressor()
            )

            X_train = catboost_frame(
                train,
                numeric_features,
            )

            X_eval = catboost_frame(
                evaluation,
                numeric_features,
            )

            model.fit(
                X_train,
                y_train,
                cat_features=(
                    CATEGORICAL_FEATURES
                ),
            )

        else:

            model = regressor_factory(
                model_name,
                numeric_features,
            )

            X_train = train
            X_eval = evaluation

            model.fit(
                X_train,
                y_train,
            )

        prediction = np.maximum(
            model.predict(
                X_eval
            ),
            0,
        )

        mae = mean_absolute_error(
            y_eval,
            prediction,
        )

        rmse = np.sqrt(
            mean_squared_error(
                y_eval,
                prediction,
            )
        )

        r2 = r2_score(
            y_eval,
            prediction,
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

                "mae":
                    float(mae),

                "rmse":
                    float(rmse),

                "r2":
                    float(r2),
            }
        )

    return rows


def aggregate_regressor(
    rows,
):

    mae = np.array(
        [
            row["mae"]
            for row in rows
        ]
    )

    rmse = np.array(
        [
            row["rmse"]
            for row in rows
        ]
    )

    return {
        "fold_count":
            int(len(rows)),

        "median_mae":
            float(
                np.median(mae)
            ),

        "worst_mae":
            float(
                mae.max()
            ),

        "mae_std":
            float(
                mae.std()
            ),

        "median_rmse":
            float(
                np.median(rmse)
            ),
    }


def regression_key(
    aggregate,
):

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
        "RECOVERIQ — FULL CATEGORICAL MODEL TOURNAMENT"
    )

    dev = add_targets(
        load_development_data()
    )

    dev = add_categorical_features(
        dev
    )

    folds = build_temporal_folds(
        dev
    )

    print()
    print(
        f"Development rows: {len(dev):,}"
    )

    print(
        "TEST SPLIT WAS NOT READ."
    )

    models = [
        "random_forest",
        "gradient_boosting",
        "xgboost",
        "lightgbm",
        "catboost",
    ]

    report = {
        "categorical_features":
            CATEGORICAL_FEATURES,

        "classification": {},

        "regression": {},

        "test_used":
            False,
    }


    # ========================================================
    # CLASSIFICATION
    # ========================================================

    for head_name in [
        "late_payment",
        "delay_over_7d",
    ]:

        banner(
            f"CLASSIFICATION — {head_name.upper()}"
        )

        all_candidates = []

        for model_name in models:

            best = None

            print()
            print(
                f"{model_name.upper()}"
            )

            for (
                feature_set_name,
                numeric_features,
            ) in FEATURE_SETS.items():

                fold_rows = (
                    evaluate_classifier(
                        dev=dev,
                        folds=folds,
                        head_name=head_name,
                        numeric_features=(
                            numeric_features
                        ),
                        model_name=model_name,
                    )
                )

                if not fold_rows:
                    continue

                aggregate = (
                    aggregate_classifier(
                        fold_rows
                    )
                )

                candidate = {
                    "model":
                        model_name,

                    "feature_set":
                        feature_set_name,

                    "aggregate":
                        aggregate,

                    "folds":
                        fold_rows,
                }

                print(
                    f"  {feature_set_name:<20}"
                    f"median="
                    f"{aggregate['median_roc_auc']:.4f}  "
                    f"worst="
                    f"{aggregate['worst_roc_auc']:.4f}  "
                    f"std="
                    f"{aggregate['roc_auc_std']:.4f}  "
                    f"below50="
                    f"{aggregate['below_random_folds']}  "
                    f"PRlift="
                    f"{aggregate['median_pr_lift']:.2f}"
                )

                if (
                    best is None
                    or classifier_key(
                        aggregate
                    )
                    <
                    classifier_key(
                        best["aggregate"]
                    )
                ):
                    best = candidate

            if best is not None:

                all_candidates.append(
                    best
                )

                print(
                    "  BEST ->",
                    best[
                        "feature_set"
                    ],
                )

        ranked = sorted(
            all_candidates,
            key=lambda x:
                classifier_key(
                    x["aggregate"]
                ),
        )

        winner = ranked[0]

        banner(
            f"{head_name.upper()} LEADERBOARD"
        )

        for rank, candidate in enumerate(
            ranked,
            start=1,
        ):

            a = candidate[
                "aggregate"
            ]

            print(
                f"{rank}. "
                f"{candidate['model']:<20}"
                f"{candidate['feature_set']:<20}"
                f"median={a['median_roc_auc']:.4f}  "
                f"worst={a['worst_roc_auc']:.4f}  "
                f"std={a['roc_auc_std']:.4f}  "
                f"PRlift={a['median_pr_lift']:.2f}"
            )

        print()
        print(
            "CATEGORICAL TOURNAMENT WINNER:",
            winner["model"],
            "/",
            winner["feature_set"],
        )

        report[
            "classification"
        ][
            head_name
        ] = {
            "ranking":
                ranked,

            "winner":
                winner,
        }


    # ========================================================
    # CONDITIONAL LATENESS REGRESSION
    # ========================================================

    banner(
        "REGRESSION — DAYS LATE IF LATE"
    )

    regression_candidates = []

    for model_name in models:

        best = None

        print()
        print(
            model_name.upper()
        )

        for (
            feature_set_name,
            numeric_features,
        ) in FEATURE_SETS.items():

            fold_rows = (
                evaluate_regressor(
                    dev=dev,
                    folds=folds,
                    numeric_features=(
                        numeric_features
                    ),
                    model_name=model_name,
                )
            )

            if not fold_rows:
                continue

            aggregate = (
                aggregate_regressor(
                    fold_rows
                )
            )

            candidate = {
                "model":
                    model_name,

                "feature_set":
                    feature_set_name,

                "aggregate":
                    aggregate,

                "folds":
                    fold_rows,
            }

            print(
                f"  {feature_set_name:<20}"
                f"median_MAE="
                f"{aggregate['median_mae']:.3f}  "
                f"worst="
                f"{aggregate['worst_mae']:.3f}  "
                f"std="
                f"{aggregate['mae_std']:.3f}"
            )

            if (
                best is None
                or regression_key(
                    aggregate
                )
                <
                regression_key(
                    best["aggregate"]
                )
            ):
                best = candidate

        if best is not None:

            regression_candidates.append(
                best
            )

    ranked_regression = sorted(
        regression_candidates,
        key=lambda x:
            regression_key(
                x["aggregate"]
            ),
    )

    banner(
        "DAYS-LATE REGRESSION LEADERBOARD"
    )

    for rank, candidate in enumerate(
        ranked_regression,
        start=1,
    ):

        a = candidate[
            "aggregate"
        ]

        print(
            f"{rank}. "
            f"{candidate['model']:<20}"
            f"{candidate['feature_set']:<20}"
            f"median_MAE="
            f"{a['median_mae']:.3f}  "
            f"worst="
            f"{a['worst_mae']:.3f}  "
            f"std="
            f"{a['mae_std']:.3f}"
        )

    report[
        "regression"
    ] = {
        "ranking":
            ranked_regression,

        "winner":
            ranked_regression[0],
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
        "CATEGORICAL MODEL TOURNAMENT COMPLETE"
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
        "NO PRODUCTION MODEL WAS OVERWRITTEN."
    )

    print(
        "TEST SPLIT WAS NEVER READ."
    )


if __name__ == "__main__":
    main()
