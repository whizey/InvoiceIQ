"""Temporal stability benchmark for InvoiceIQ B2B late-payment models."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import (
    GradientBoostingClassifier,
)
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.metrics import (
    roc_auc_score,
    average_precision_score,
    brier_score_loss,
)

from app.ml.common import (
    FEATURES,
    TARGET_COLUMN,
    SPLIT_DIR,
    temporal_folds,
)

OUTPUT = Path("app/ml/artifacts/benchmarks/payment_risk")
MODELS = (
    "random_forest",
    "gradient_boosting",
    "xgboost",
    "lightgbm",
    "catboost",
)


def build_model(name, threads):
    if name == "random_forest":
        from sklearn.ensemble import RandomForestClassifier
        model = RandomForestClassifier(
            n_estimators=200,
            min_samples_leaf=10,
            n_jobs=threads,
            random_state=42,
        )

    elif name == "gradient_boosting":
        model = GradientBoostingClassifier(
            n_estimators=200,
            learning_rate=0.04,
            max_depth=3,
            min_samples_leaf=20,
            random_state=42,
        )

    elif name == "xgboost":
        from xgboost import XGBClassifier
        model = XGBClassifier(
            n_estimators=300,
            max_depth=3,
            learning_rate=0.04,
            min_child_weight=15,
            subsample=0.85,
            colsample_bytree=0.85,
            reg_lambda=5,
            tree_method="hist",
            n_jobs=threads,
            random_state=42,
            eval_metric="logloss",
        )

    elif name == "lightgbm":
        from lightgbm import LGBMClassifier
        model = LGBMClassifier(
            n_estimators=300,
            num_leaves=15,
            learning_rate=0.04,
            min_child_samples=30,
            reg_lambda=5,
            n_jobs=threads,
            verbosity=-1,
            random_state=42,
        )

    elif name == "catboost":
        from catboost import CatBoostClassifier
        model = CatBoostClassifier(
            iterations=300,
            depth=4,
            learning_rate=0.04,
            l2_leaf_reg=5,
            thread_count=threads,
            verbose=False,
            allow_writing_files=False,
            random_seed=42,
        )

    else:
        raise ValueError(f"Unknown model: {name}")

    return make_pipeline(
        SimpleImputer(
            strategy="median",
            keep_empty_features=True,
        ),
        model,
    )


def load_split(name):
    path = SPLIT_DIR / f"{name}.csv.gz"
    if not path.exists():
        raise FileNotFoundError(f"Missing: {path}")

    df = pd.read_csv(path)

    required = set(FEATURES) | {
        TARGET_COLUMN,
        "posting_date",
        "net_due_date",
        "days_late_strict",
        "prior_resolved_invoice_count",
    }
    missing = required - set(df.columns)

    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    df["posting_date"] = pd.to_datetime(df["posting_date"])
    df["net_due_date"] = pd.to_datetime(df["net_due_date"])

    # The processed dataset intentionally excludes clearing_date.
    # Reconstruct it from audit-only columns to prevent training
    # on outcomes that were not yet known at a fold's cutoff.
    # Never include these columns in FEATURES.
    days_late = pd.to_numeric(
        df["days_late_strict"], errors="raise"
    )

    if days_late.isna().any():
        raise ValueError("Missing days_late_strict audit values")

    df["clearing_date"] = (
        df["net_due_date"]
        + pd.to_timedelta(days_late, unit="D")
    )

    if df[[TARGET_COLUMN, "posting_date", "clearing_date"]].isna().any().any():
        raise ValueError("Missing labels or dates in split")

    for feature in FEATURES:
        df[feature] = pd.to_numeric(
            df[feature], errors="coerce"
        )

    df[FEATURES] = df[FEATURES].replace(
        [np.inf, -np.inf], np.nan
    )

    return df.sort_values("posting_date").reset_index(drop=True)


def metrics(df, probabilities):
    y = df[TARGET_COLUMN].astype(int).to_numpy()

    if len(y) < 2 or len(np.unique(y)) < 2:
        return None

    months = df["posting_date"].dt.to_period("M")
    monthly_scores = []
    monthly_weights = []

    for month in months.unique():
        mask = (months == month).to_numpy()
        if len(np.unique(y[mask])) < 2:
            continue
        monthly_scores.append(
            roc_auc_score(y[mask], probabilities[mask])
        )
        monthly_weights.append(int(mask.sum()))

    within_auc = (
        float(np.average(monthly_scores, weights=monthly_weights))
        if monthly_scores else None
    )

    return {
        "within_period_auc": within_auc,
        "pooled_auc": float(roc_auc_score(y, probabilities)),
        "pr_auc": float(average_precision_score(y, probabilities)),
        "brier": float(brier_score_loss(y, probabilities)),
        "rows": int(len(df)),
        "late_rate": float(np.mean(y)),
    }


def known_outcomes(df, cutoff):
    """Return invoices whose binary lateness label was known at cutoff.

    A label is known if an invoice cleared before cutoff, or if
    its weekend-adjusted due date passed while it was still open.
    Invoices not yet due and not cleared remain excluded.
    """
    cutoff = pd.Timestamp(cutoff)

    weekday = df["net_due_date"].dt.weekday
    extension = weekday.map({5: 2, 6: 1}).fillna(0).astype(int)

    adjusted_due = (
        df["net_due_date"]
        + pd.to_timedelta(extension, unit="D")
    )

    cleared_before = df["clearing_date"] < cutoff

    unpaid_and_already_late = (
        (adjusted_due < cutoff)
        & (df["clearing_date"] >= cutoff)
    )

    # If unpaid beyond its adjusted due date, eventual lateness
    # must be positive. Check this using the retrospective audit
    # label, but never use future labels as model features.
    invalid = unpaid_and_already_late & df[TARGET_COLUMN].ne(1)
    if invalid.any():
        raise ValueError(
            f"{int(invalid.sum())} inconsistent late-payment labels"
        )

    known = (
        (df["posting_date"] < cutoff)
        & (cleared_before | unpaid_and_already_late)
    )
    return df.loc[known].copy()


def train_and_predict(name, train, evaluation, threads):
    model = build_model(name, threads)
    model.fit(train[FEATURES], train[TARGET_COLUMN].astype(int))

    probabilities = model.predict_proba(
        evaluation[FEATURES]
    )[:, 1]

    return probabilities


def benchmark(name, train, validation, threads):
    fold_results = []

    print(f"\n=== {name.upper()} ===", flush=True)

    for index, (train_idx, eval_idx) in enumerate(temporal_folds(train)):
        evaluation = train.iloc[eval_idx].copy()
        cutoff = evaluation["posting_date"].min()

        historical = known_outcomes(
            train.iloc[train_idx], cutoff
        )

        if len(historical) < 100 or historical[TARGET_COLUMN].nunique() < 2:
            print(f"Fold {index}: insufficient observed labels")
            continue

        probabilities = train_and_predict(
            name, historical, evaluation, threads
        )

        overall = metrics(evaluation, probabilities)

        history_mask = (
            evaluation["prior_resolved_invoice_count"] >= 3
        ).to_numpy()

        experienced = metrics(
            evaluation.loc[history_mask].copy(),
            probabilities[history_mask],
        )

        row = {
            "fold": index,
            "cutoff": str(cutoff.date()),
            "training_rows": int(len(historical)),
            "all_invoices": overall,
            "history_eligible": experienced,
        }

        fold_results.append(row)

        auc = overall["within_period_auc"] if overall else None
        print(
            f"Fold {index}: "
            f"train={len(historical)}, "
            f"eval={len(evaluation)}, "
            f"AUC={auc}",
            flush=True,
        )

    cutoff = validation["posting_date"].min()
    final_train = known_outcomes(train, cutoff)

    if final_train[TARGET_COLUMN].nunique() < 2:
        raise ValueError("Insufficient observed labels for validation")

    probabilities = train_and_predict(
        name, final_train, validation, threads
    )
    validation_metrics = metrics(validation, probabilities)

    fold_aucs = [
        row["all_invoices"]["within_period_auc"]
        for row in fold_results
        if row["all_invoices"]
        and row["all_invoices"]["within_period_auc"] is not None
    ]

    return {
        "temporal_folds": fold_results,
        "temporal_mean_auc": (
            float(np.mean(fold_aucs)) if fold_aucs else None
        ),
        "temporal_min_auc": (
            float(np.min(fold_aucs)) if fold_aucs else None
        ),
        "temporal_std_auc": (
            float(np.std(fold_aucs)) if fold_aucs else None
        ),
        "inverted_folds": sum(x < 0.5 for x in fold_aucs),
        "validation": validation_metrics,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        nargs="+",
        choices=MODELS,
        default=list(MODELS),
    )
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()

    if args.threads < 1:
        parser.error("--threads must be at least 1")

    train = load_split("train")
    validation = load_split("validation")

    if train["posting_date"].max() >= validation["posting_date"].min():
        raise ValueError("Train and validation periods overlap")

    print(f"Train invoices: {len(train)}")
    print(f"Validation invoices: {len(validation)}")
    print(f"Features: {len(FEATURES)}")
    print(f"Target: {TARGET_COLUMN}")
    print("Reserved test split: NOT READ")
    print("Production model: UNCHANGED")

    results = {}

    for name in args.models:
        results[name] = benchmark(
            name, train, validation, args.threads
        )

    OUTPUT.mkdir(parents=True, exist_ok=True)

    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "target": TARGET_COLUMN,
        "features": FEATURES,
        "test_split_read": False,
        "production_model_unchanged": True,
        "results": results,
    }

    (OUTPUT / "comparison.json").write_text(
        json.dumps(report, indent=2, allow_nan=False)
    )

    rows = []

    for name, result in results.items():
        validation_result = result["validation"] or {}

        rows.append({
            "model": name,
            "validation_auc": validation_result.get("within_period_auc"),
            "validation_pr_auc": validation_result.get("pr_auc"),
            "validation_brier": validation_result.get("brier"),
            "temporal_mean_auc": result["temporal_mean_auc"],
            "temporal_min_auc": result["temporal_min_auc"],
            "temporal_std_auc": result["temporal_std_auc"],
            "inverted_folds": result["inverted_folds"],
        })

    summary = pd.DataFrame(rows)
    summary.to_csv(OUTPUT / "comparison.csv", index=False)

    print("\n=== TEMPORAL STABILITY COMPARISON ===")
    print(summary.to_string(index=False))
    print(f"\nSaved reports in: {OUTPUT}")
    print("No model has been deployed or promoted.")


if __name__ == "__main__":
    main()
