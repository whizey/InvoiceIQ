"""Temporal model validation for InvoiceIQ.

B2B: Late-payment classification.
BPI: Experimental process-clearance survival analysis.

No production artifacts are overwritten.
No reserved B2B test split is loaded.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
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

OUT = Path("app/ml/artifacts/stage3_validation")


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, allow_nan=False)
    )


def plot_setup():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def load_b2b_split(name):
    path = SPLIT_DIR / f"{name}.csv.gz"

    df = pd.read_csv(path)
    df["posting_date"] = pd.to_datetime(df["posting_date"])
    df["net_due_date"] = pd.to_datetime(df["net_due_date"])

    df["clearing_date"] = (
        df["net_due_date"]
        + pd.to_timedelta(
            pd.to_numeric(df["days_late_strict"]),
            unit="D",
        )
    )

    for col in FEATURES:
        df[col] = pd.to_numeric(
            df[col], errors="coerce"
        ).replace([np.inf, -np.inf], np.nan)

    return df.sort_values(
        "posting_date", kind="stable"
    ).reset_index(drop=True)


def adjusted_due_date(df):
    weekday = df["net_due_date"].dt.weekday

    extension = weekday.map({
        5: 2,
        6: 1,
    }).fillna(0).astype(int)

    return (
        df["net_due_date"]
        + pd.to_timedelta(extension, unit="D")
    )


def known_outcomes(df, cutoff):
    cutoff = pd.Timestamp(cutoff)

    cleared = df["clearing_date"] < cutoff

    overdue = (
        (adjusted_due_date(df) < cutoff)
        & (df["clearing_date"] >= cutoff)
    )

    invalid = overdue & df[TARGET_COLUMN].ne(1)
    if invalid.any():
        raise ValueError(
            "Inconsistent overdue labels: "
            f"{int(invalid.sum())}"
        )

    return df.loc[
        (df["posting_date"] < cutoff)
        & (cleared | overdue)
    ].copy()


def make_model(name, threads):
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline

    if name == "random_forest":
        from sklearn.ensemble import RandomForestClassifier

        estimator = RandomForestClassifier(
            n_estimators=200,
            min_samples_leaf=10,
            random_state=42,
            n_jobs=threads,
        )

    elif name == "lightgbm":
        from lightgbm import LGBMClassifier

        estimator = LGBMClassifier(
            n_estimators=300,
            num_leaves=15,
            learning_rate=0.04,
            min_child_samples=30,
            reg_lambda=5,
            n_jobs=threads,
            random_state=42,
            verbosity=-1,
        )

    else:
        raise ValueError(name)

    return make_pipeline(
        SimpleImputer(
            strategy="median",
            keep_empty_features=True,
        ),
        estimator,
    )


def metrics(y, probabilities):
    y = np.asarray(y, dtype=int)
    p = np.asarray(probabilities, dtype=float)

    return {
        "rows": int(len(y)),
        "late_rate": float(y.mean()),
        "auc": (
            float(roc_auc_score(y, p))
            if len(np.unique(y)) > 1 else None
        ),
        "pr_auc": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
    }


def reliability_table(y, probabilities, bins=10):
    frame = pd.DataFrame({
        "y": np.asarray(y),
        "p": np.asarray(probabilities),
    })

    frame["bin"] = np.minimum(
        (frame["p"] * bins).astype(int),
        bins - 1,
    )

    return frame.groupby("bin").agg(
        predicted=("p", "mean"),
        observed=("y", "mean"),
        count=("y", "size"),
    ).reset_index()


def run_b2b(threads):
    train = load_b2b_split("train")
    validation = load_b2b_split("validation")

    if train["posting_date"].max() >= validation["posting_date"].min():
        raise ValueError("Overlapping train and validation periods")

    output = OUT / "b2b"
    output.mkdir(parents=True, exist_ok=True)

    fold_results = []
    predictions = []

    for name in ("random_forest", "lightgbm"):
        print(f"\n=== {name.upper()} ===", flush=True)

        for fold, (train_idx, eval_idx) in enumerate(
            temporal_folds(train)
        ):
            evaluation = train.iloc[eval_idx].copy()
            cutoff = evaluation["posting_date"].min()

            historical = known_outcomes(
                train.iloc[train_idx],
                cutoff,
            )

            if (
                len(historical) < 100
                or historical[TARGET_COLUMN].nunique() < 2
            ):
                raise ValueError(
                    f"Insufficient labels in fold {fold}"
                )

            model = make_model(name, threads)
            model.fit(
                historical[FEATURES],
                historical[TARGET_COLUMN].astype(int),
            )

            p = model.predict_proba(
                evaluation[FEATURES]
            )[:, 1]

            result = metrics(
                evaluation[TARGET_COLUMN],
                p,
            )

            monthly_scores = []
            monthly_weights = []

            months = evaluation["posting_date"].dt.to_period("M")

            for month in months.unique():
                mask = (months == month).to_numpy()
                y_month = evaluation.loc[
                    mask, TARGET_COLUMN
                ].to_numpy()

                if len(np.unique(y_month)) < 2:
                    continue

                monthly_scores.append(
                    roc_auc_score(y_month, p[mask])
                )
                monthly_weights.append(int(mask.sum()))

            result["within_month_auc"] = (
                float(np.average(
                    monthly_scores,
                    weights=monthly_weights,
                ))
                if monthly_scores else None
            )

            result.update({
                "model": name,
                "fold": fold,
                "train_rows": len(historical),
                "cutoff": str(cutoff.date()),
            })

            fold_results.append(result)

            predictions.append(pd.DataFrame({
                "model": name,
                "fold": fold,
                "month": months.astype(str).to_numpy(),
                "y": evaluation[TARGET_COLUMN].to_numpy(),
                "p": p,
                "history_count": evaluation[
                    "prior_resolved_invoice_count"
                ].to_numpy(),
            }))

            print(
                f"Fold {fold}: "
                f"train={len(historical)}, "
                f"AUC={result['within_month_auc']}",
                flush=True,
            )

    scores = pd.DataFrame(fold_results)
    pred = pd.concat(predictions, ignore_index=True)

    scores.to_csv(output / "temporal_metrics.csv", index=False)
    pred.to_csv(
        output / "temporal_predictions.csv.gz",
        index=False,
        compression="gzip",
    )

    plt = plot_setup()

    # Plot 1: temporal stability
    fig, ax = plt.subplots(figsize=(8, 5))

    for name, group in scores.groupby("model"):
        ax.plot(
            group["fold"],
            group["within_month_auc"],
            marker="o",
            label=name,
        )

    ax.axhline(0.5, linestyle="--", color="gray")
    ax.set(
        title="B2B Temporal Model Stability",
        xlabel="Temporal Fold",
        ylabel="Within-month ROC-AUC",
    )
    ax.set_ylim(0, 1)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output / "temporal_auc.png", dpi=160)
    plt.close(fig)

    # Plot 2: out-of-fold calibration
    fig, ax = plt.subplots(figsize=(7, 6))

    for name, group in pred.groupby("model"):
        reliability = reliability_table(
            group["y"], group["p"]
        )

        ax.plot(
            reliability["predicted"],
            reliability["observed"],
            marker="o",
            label=name,
        )

    ax.plot([0, 1], [0, 1], "--", color="gray")
    ax.set(
        title="B2B Out-of-Time Reliability",
        xlabel="Predicted late-payment probability",
        ylabel="Observed late-payment rate",
    )
    ax.legend()
    fig.tight_layout()
    fig.savefig(output / "reliability.png", dpi=160)
    plt.close(fig)

    # Plot 3: observed monthly label drift
    fig, ax = plt.subplots(figsize=(10, 5))

    observed = (
        train.groupby(
            train["posting_date"].dt.to_period("M")
        )[TARGET_COLUMN].mean()
    )

    ax.plot(
        observed.index.astype(str),
        observed.values,
        marker="o",
    )
    ax.set(
        title="Monthly Late-Payment Rate",
        xlabel="Posting Month",
        ylabel="Observed Late-Payment Rate",
    )
    ax.tick_params(axis="x", rotation=60)
    fig.tight_layout()
    fig.savefig(output / "monthly_drift.png", dpi=160)
    plt.close(fig)

    summary = {}

    for name, group in scores.groupby("model"):
        values = group["within_month_auc"].dropna()

        subset = pred[pred["model"] == name]

        summary[name] = {
            "mean_temporal_auc": float(values.mean()),
            "worst_temporal_auc": float(values.min()),
            "inverted_folds": int((values < 0.5).sum()),
            "pooled_oof_brier": float(
                brier_score_loss(subset["y"], subset["p"])
            ),
        }

    save_json(output / "report.json", {
        "target": TARGET_COLUMN,
        "features": list(FEATURES),
        "models": summary,
        "validation_rows": len(validation),
        "reserved_test_split_read": False,
        "status": "experimental",
        "limitations": [
            "Four temporal folds",
            "Only 69 customers in the primary B2B dataset",
            "Retrospectively observed cleared-invoice population",
            "No model automatically promoted",
        ],
    })

    print("\n=== B2B VALIDATION SUMMARY ===")
    print(scores.to_string(index=False))
    print("\nPlots saved to:", output)


def landmark_masks(duration, event, age, horizon):
    duration = np.asarray(duration, dtype=float)
    event = np.asarray(event, dtype=bool)

    at_risk = np.isfinite(duration) & (duration > age)

    positive = (
        at_risk
        & event
        & (duration <= age + horizon)
    )

    known_negative = (
        at_risk
        & (duration >= age + horizon)
        & ~positive
    )

    known = positive | known_negative

    return at_risk, known, positive


def landmark_metrics(duration, event, probability, age, horizon):
    at_risk, known, positive = landmark_masks(
        duration, event, age, horizon
    )

    result = {
        "age": age,
        "horizon": horizon,
        "at_risk": int(at_risk.sum()),
        "known": int(known.sum()),
        "coverage": float(
            known.sum() / max(at_risk.sum(), 1)
        ),
        "auc": None,
        "brier": None,
        "predicted_mean": None,
        "observed_rate": None,
    }

    if not known.any():
        return result

    y = positive[known].astype(int)
    p = np.asarray(probability)[known]

    result.update({
        "brier": float(brier_score_loss(y, p)),
        "auc": (
            float(roc_auc_score(y, p))
            if len(np.unique(y)) > 1 else None
        ),
        "predicted_mean": float(p.mean()),
        "observed_rate": float(y.mean()),
    })

    return result


def run_bpi(fold_name, threads, batch_size, max_eval):
    from app.ml.train_bpi_survival import (
        FOLDS,
        available_features,
        expand_hazard_rows,
        load_data,
        make_lightgbm,
        predict_recovery_curves,
        recensor_training_data,
        utc,
    )
    from app.ml.bpi_conditional_survival import (
        conditional_clearance_probabilities,
    )

    fold = next(
        (f for f in FOLDS if f["name"] == fold_name),
        None,
    )

    if fold is None:
        raise ValueError(f"Unknown fold: {fold_name}")

    df = load_data()

    training = recensor_training_data(
        df, fold["train_cutoff"]
    )

    evaluation = df[
        (df["invoice_timestamp"] >= utc(fold["eval_start"]))
        & (df["invoice_timestamp"] < utc(fold["eval_end"]))
    ].copy()

    if max_eval:
        evaluation = (
            evaluation.sort_values("invoice_timestamp")
            .head(max_eval)
            .copy()
        )

    if evaluation.empty:
        raise ValueError("No evaluation invoices")

    numeric, categorical = available_features(training)

    expanded = expand_hazard_rows(
        training, "_fold_duration", "_fold_event"
    )

    model = make_lightgbm(numeric, categorical)
    model.named_steps["model"].set_params(n_jobs=threads)

    model.fit(
        expanded,
        expanded["hazard_event"].astype(int),
    )

    del expanded, training

    duration = evaluation["duration_days"].to_numpy(
        dtype=float
    )
    event = evaluation["event_observed"].to_numpy(
        dtype=int
    )

    combinations = [
        (age, horizon)
        for age in (15, 30, 60)
        for horizon in (7, 30)
    ]

    probabilities = {
        item: np.full(len(evaluation), np.nan)
        for item in combinations
    }

    for start in range(0, len(evaluation), batch_size):
        end = min(start + batch_size, len(evaluation))

        batch = evaluation.iloc[start:end].reset_index(
            drop=True
        )

        _, _, cdf = predict_recovery_curves(model, batch)

        for position, (_, curve) in enumerate(cdf.iterrows()):
            idx = start + position

            base_curve = {
                float(t): float(p)
                for t, p in curve.items()
            }

            for age in (15, 30, 60):
                if duration[idx] <= age:
                    continue

                try:
                    conditional = conditional_clearance_probabilities(
                        base_curve,
                        elapsed_days_since_receipt=float(age),
                        horizons=(7, 30),
                    )
                except ValueError:
                    continue

                for horizon in (7, 30):
                    probabilities[(age, horizon)][idx] = (
                        conditional[
                            f"clearance_probability_next_{horizon}d"
                        ]
                    )

        print(
            f"BPI scored {end}/{len(evaluation)}",
            flush=True,
        )

    output = OUT / "bpi" / fold_name
    output.mkdir(parents=True, exist_ok=True)

    rows = []
    known_predictions = []

    for age, horizon in combinations:
        p = probabilities[(age, horizon)]
        valid = np.isfinite(p)

        at_risk, known, positive = landmark_masks(
            duration, event, age, horizon
        )

        usable = valid & known

        if usable.any():
            summary = landmark_metrics(
                duration[valid],
                event[valid],
                p[valid],
                age,
                horizon,
            )

            known_predictions.append(pd.DataFrame({
                "age": age,
                "horizon": horizon,
                "y": positive[usable].astype(int),
                "p": p[usable],
            }))
        else:
            summary = {
                "age": age,
                "horizon": horizon,
                "at_risk": int(at_risk.sum()),
                "known": 0,
                "coverage": 0.0,
                "auc": None,
                "brier": None,
                "predicted_mean": None,
                "observed_rate": None,
            }

        rows.append(summary)

    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(
        output / "landmark_metrics.csv",
        index=False,
    )

    if known_predictions:
        combined = pd.concat(
            known_predictions,
            ignore_index=True,
        )

        plt = plot_setup()
        fig, ax = plt.subplots(figsize=(8, 6))

        for (age, horizon), group in combined.groupby(
            ["age", "horizon"]
        ):
            bins = reliability_table(group["y"], group["p"])

            ax.plot(
                bins["predicted"],
                bins["observed"],
                marker="o",
                label=f"Age {age}, next {horizon}d",
            )

        ax.plot([0, 1], [0, 1], "--", color="gray")
        ax.set(
            title="BPI Conditional Clearance Calibration",
            xlabel="Predicted clearance probability",
            ylabel="Observed clearance rate",
        )
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(
            output / "landmark_reliability.png",
            dpi=160,
        )
        plt.close(fig)

    save_json(output / "report.json", {
        "dataset": "BPI Challenge 2019",
        "target": "Purchase-to-pay process clearance",
        "fold": fold_name,
        "evaluation_rows": len(evaluation),
        "partial_sample": max_eval is not None,
        "status": "experimental",
        "landmarks": rows,
        "limitation": (
            "Complete-case metrics exclude insufficient follow-up. "
            "They are not IPCW-adjusted population calibration "
            "estimates. Process clearance is not cash recovery."
        ),
    })

    print("\n=== BPI LANDMARK VALIDATION ===")
    print(summary_df.to_string(index=False))
    print("\nPlots saved to:", output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--track",
        choices=["b2b", "bpi"],
        required=True,
    )
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--fold", default="fold_0")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-eval", type=int)

    args = parser.parse_args()

    if args.threads < 1 or args.batch_size < 1:
        parser.error("threads and batch-size must be positive")

    if args.max_eval is not None and args.max_eval < 1:
        parser.error("max-eval must be positive")

    if args.track == "b2b":
        run_b2b(args.threads)
    else:
        run_bpi(
            args.fold,
            args.threads,
            args.batch_size,
            args.max_eval,
        )


if __name__ == "__main__":
    main()
