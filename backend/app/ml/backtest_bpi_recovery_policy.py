"""Temporal BPI survival and recovery-policy backtest."""

from __future__ import annotations

import argparse
import gc
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.ml import train_bpi_survival as survival
from app.services.bpi_recovery_policy import decide_recovery_action


ARTIFACTS = Path(__file__).resolve().parent / "artifacts" / "bpi_survival"
HORIZONS = (7, 30, 60, 90)


def enum_value(value):
    return str(getattr(value, "value", value))


def group_summary(frame, key):
    result = []

    for category, part in frame.groupby(key, dropna=False, sort=True):
        item = {
            key: str(category),
            "invoices": int(len(part)),
        }

        for days in HORIZONS:
            labels = pd.to_numeric(
                part[f"clear_within_{days}d"],
                errors="coerce",
            )
            known = labels.notna()
            count = int(known.sum())

            item[f"known_{days}d"] = count
            item[f"observed_clearance_rate_{days}d"] = (
                round(float(labels[known].mean()), 6)
                if count else None
            )
            item[f"mean_predicted_clearance_{days}d_on_known"] = (
                round(float(part.loc[known, f"p{days}"].mean()), 6)
                if count else None
            )

        result.append(item)

    return result


def summarize(frame):
    tiers = group_summary(frame, "risk_tier")
    actions = group_summary(frame, "recommended_action")

    rates = {
        row["risk_tier"]: row["observed_clearance_rate_30d"]
        for row in tiers
    }

    if all(rates.get(t) is not None for t in ("LOW", "MEDIUM", "HIGH")):
        order = (
            rates["LOW"] >= rates["MEDIUM"] >= rates["HIGH"]
        )
    else:
        order = None

    return {
        "rows": int(len(frame)),
        "by_tier": tiers,
        "by_recommended_action": actions,
        "30d_low_to_high_order_observed": order,
        "30d_order_interpretation": (
            "Observational stratification, not a causal action effect."
        ),
    }


def score_fold(source, fold, numeric, categorical, batch_size, threads):
    name = fold["name"]
    cutoff = survival.utc(fold["train_cutoff"])
    start = survival.utc(fold["eval_start"])
    end = survival.utc(fold["eval_end"])

    print(
        f"\n{name}: cutoff={cutoff}, evaluation=[{start}, {end})",
        flush=True,
    )

    train_cases = survival.recensor_training_data(source, cutoff)

    train_hazard = survival.expand_hazard_rows(
        train_cases,
        "_fold_duration",
        "_fold_event",
    )

    evaluation = source.loc[
        (source["invoice_timestamp"] >= start)
        & (source["invoice_timestamp"] < end)
    ].copy()

    evaluation["_source_row"] = evaluation.index.astype(int)
    evaluation = evaluation.reset_index(drop=True)

    if evaluation.empty or train_hazard.empty:
        raise ValueError(f"{name}: empty training or evaluation data")

    print(
        f"{name}: {len(train_cases):,} training invoices, "
        f"{len(train_hazard):,} hazard rows, "
        f"{len(evaluation):,} evaluation invoices",
        flush=True,
    )

    model = survival.make_lightgbm(numeric, categorical)
    model.set_params(model__n_jobs=threads)

    model.fit(
        train_hazard,
        train_hazard["hazard_event"].astype(int),
    )

    del train_hazard, train_cases
    gc.collect()

    cdf_batches = []
    cdf_columns = [float(h) for h in survival.HORIZONS]

    for start_row in range(0, len(evaluation), batch_size):
        batch = evaluation.iloc[
            start_row:start_row + batch_size
        ].reset_index(drop=True)

        _, _, cdf = survival.predict_recovery_curves(model, batch)
        selected = cdf.reindex(columns=cdf_columns)

        if selected.isna().any().any():
            raise RuntimeError(f"{name}: missing predicted horizon")

        cdf_batches.append(
            selected.to_numpy(dtype=float, copy=True)
        )

        print(
            f"{name}: scored "
            f"{min(start_row + batch_size, len(evaluation)):,}"
            f"/{len(evaluation):,}",
            flush=True,
        )

    del model
    gc.collect()

    curve = pd.DataFrame(
        np.vstack(cdf_batches),
        columns=cdf_columns,
    )

    if not np.isfinite(curve.to_numpy()).all():
        raise ValueError(f"{name}: nonfinite probabilities")

    if (np.diff(curve.to_numpy(), axis=1) < -1e-9).any():
        raise ValueError(f"{name}: nonmonotonic clearance curve")

    output = pd.DataFrame({
        "fold": name,
        "source_row": evaluation["_source_row"],
        "invoice_timestamp": evaluation["invoice_timestamp"].astype(str),
    })

    for days in HORIZONS:
        output[f"p{days}"] = curve[float(days)].to_numpy()

        target = f"clear_within_{days}d"
        if target not in evaluation:
            raise KeyError(f"Missing outcome: {target}")

        output[target] = pd.to_numeric(
            evaluation[target],
            errors="coerce",
        )

    decisions = []

    probabilities = output[
        ["p7", "p30", "p60", "p90"]
    ].itertuples(index=False, name=None)

    for p7, p30, p60, p90 in probabilities:
        decision = decide_recovery_action(
            clearance_probability_7d=float(p7),
            clearance_probability_30d=float(p30),
            clearance_probability_60d=float(p60),
            clearance_probability_90d=float(p90),
            amount_outstanding=0.0,
            days_overdue=0,
            reminders_sent=0,
            promise_to_pay_active=False,
        )

        decisions.append((
            enum_value(decision.risk_tier),
            enum_value(decision.recommended_action),
            round(float(decision.urgency_score), 4),
            bool(decision.human_review_required),
            " | ".join(decision.policy_reasons),
        ))

    (
        output["risk_tier"],
        output["recommended_action"],
        output["urgency_score"],
        output["human_review_required"],
        output["policy_reasons"],
    ) = tuple(zip(*decisions))

    fold_report = {
        "name": name,
        "train_cutoff": str(cutoff),
        "eval_start": str(start),
        "eval_end": str(end),
        "training_method": "re-censored historical LightGBM hazards",
        "threads": threads,
        "survival_horizon_metrics": survival.horizon_metrics(
            evaluation, curve
        ),
        "policy": summarize(output),
    }

    return output, fold_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--fold", default="fold_0")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()

    if args.batch_size < 1 or args.threads < 1:
        parser.error("batch-size and threads must be positive")

    folds = list(survival.FOLDS)

    if not args.all:
        folds = [
            fold for fold in folds
            if fold["name"] == args.fold
        ]
        if not folds:
            parser.error(f"Unknown fold: {args.fold}")

    print("Loading real BPI feature data", flush=True)

    source = survival.load_data()
    numeric, categorical = survival.available_features(source)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    reports = []
    paths = []

    for fold in folds:
        output, fold_report = score_fold(
            source,
            fold,
            numeric,
            categorical,
            args.batch_size,
            args.threads,
        )

        name = fold["name"]

        path = ARTIFACTS / f"policy_backtest_{name}_cases.csv.gz"

        output.to_csv(
            path,
            compression="gzip",
            index=False,
        )

        paths.append(path)

        report_path = ARTIFACTS / f"policy_backtest_{name}.json"

        report_path.write_text(
            json.dumps(fold_report, indent=2, allow_nan=False) + "\n"
        )

        reports.append(fold_report)

        print(f"Saved {name}: {path}", flush=True)

        del output
        gc.collect()

    if args.all:
        frames = [pd.read_csv(path) for path in paths]
        combined = pd.concat(frames, ignore_index=True)

        summary = {
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "evaluation_type": "temporal out-of-fold retrospective",
            "data_source": "real BPI2019 invoice receipt cohort",
            "model": "lightgbm_hazard",
            "prediction_timestamp": "Record Invoice Receipt",
            "folds": reports,
            "pooled_descriptive_policy_summary": summarize(combined),
            "policy_input_assumptions": {
                "amount_outstanding": (
                    "Unavailable as a validated current balance. "
                    "Passed as neutral zero; high-value rule not evaluated."
                ),
                "days_overdue": (
                    "Passed as zero at invoice receipt. "
                    "Post-due escalation rules not evaluated."
                ),
                "reminders_sent": "Passed as zero.",
                "promise_to_pay_active": "Passed as false.",
                "currency": "No currency or threshold conversion assumed.",
            },
            "limitations": [
                "Recommendations are hypothetical, not executed actions.",
                "Observed horizon-label availability is affected by censoring.",
                "Action effectiveness cannot be estimated from these data.",
                "BPI predictions are not validated for arbitrary B2B cases.",
            ],
        }

        (ARTIFACTS / "policy_backtest.json").write_text(
            json.dumps(summary, indent=2, allow_nan=False) + "\n"
        )

        combined.to_csv(
            ARTIFACTS / "policy_backtest_cases.csv.gz",
            compression="gzip",
            index=False,
        )

        pooled = summary["pooled_descriptive_policy_summary"]

        pd.DataFrame(pooled["by_tier"]).to_csv(
            ARTIFACTS / "policy_backtest_by_tier.csv",
            index=False,
        )

        pd.DataFrame(pooled["by_recommended_action"]).to_csv(
            ARTIFACTS / "policy_backtest_by_action.csv",
            index=False,
        )

        print("\nALL FOLDS COMPLETE", flush=True)

        for row in pooled["by_tier"]:
            print(row, flush=True)

    else:
        for row in reports[0]["policy"]["by_tier"]:
            print(row, flush=True)

    print("\nReports:", ARTIFACTS, flush=True)


if __name__ == "__main__":
    main()
