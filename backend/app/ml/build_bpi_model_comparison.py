"""Build the final BPI survival-model comparison.

This is a reporting script only.
It does not retrain models and does not change the production champion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


ROOT = Path(
    "app/ml/artifacts/bpi_survival"
)

JSON_PATH = (
    ROOT / "model_comparison.json"
)

CSV_PATH = (
    ROOT / "model_comparison.csv"
)

MD_PATH = (
    ROOT / "MODEL_COMPARISON.md"
)


MODELS = [
    {
        "rank": 1,
        "model": "LightGBM Discrete-Time Hazard",
        "slug": "lightgbm_hazard",
        "family": "Discrete-time survival",
        "training": "Full temporal folds",
        "training_cap": None,
        "headline_auc": 0.9156864938688123,
        "worst_auc": 0.8798525257578799,
        "brier": 0.07480021823576705,
        "ece": 0.055738263270804264,
        "auc_std": 0.015309611981212088,
        "status": "PRODUCTION",
        "production": True,
        "completed": True,
        "notes": (
            "Production champion. Best overall combination of "
            "discrimination, temporal stability and calibration."
        ),
    },
    {
        "rank": 2,
        "model": "XGBoost AFT",
        "slug": "xgboost_aft",
        "family": "Accelerated failure time",
        "training": "Full temporal folds",
        "training_cap": None,
        "headline_auc": 0.9006,
        "worst_auc": 0.8504,
        "brier": 0.1151,
        "ece": None,
        "auc_std": None,
        "status": "STRONG CHALLENGER",
        "production": False,
        "completed": True,
        "notes": (
            "Strongest nonlinear challenger. Very strong ranking "
            "performance, but weaker probability calibration than "
            "the LightGBM hazard model."
        ),
    },
    {
        "rank": 3,
        "model": "CoxNet Elastic-Net",
        "slug": "coxnet",
        "family": "Regularized proportional hazards",
        "training": "Full temporal folds",
        "training_cap": None,
        "headline_auc": 0.8571,
        "worst_auc": 0.7401,
        "brier": 0.0977,
        "ece": None,
        "auc_std": None,
        "status": "CHALLENGER",
        "production": False,
        "completed": True,
        "notes": (
            "Best regularized linear survival challenger. "
            "Full-data performance was almost identical to the "
            "50k-per-fold run."
        ),
    },
    {
        "rank": 4,
        "model": "Cox Proportional Hazards",
        "slug": "cox_ph",
        "family": "Classical proportional hazards",
        "training": "Full temporal folds",
        "training_cap": None,
        "headline_auc": None,
        "worst_auc": None,
        "brier": None,
        "ece": None,
        "auc_std": None,
        "status": "CLASSICAL BASELINE",
        "production": False,
        "completed": True,
        "notes": (
            "Completed all four temporal folds. Fold mean AUCs were "
            "0.8549, 0.8675, 0.8513 and 0.8108. Numerical overflow "
            "warnings appeared in later folds, so it was retained "
            "as an interpretable baseline rather than production."
        ),
    },
    {
        "rank": 5,
        "model": "Gradient Boosting Survival",
        "slug": "gradient_boosting_survival",
        "family": "Boosted survival model",
        "training": "Resource-controlled temporal folds",
        "training_cap": 25000,
        "headline_auc": 0.7989,
        "worst_auc": 0.6633,
        "brier": 0.1096,
        "ece": None,
        "auc_std": None,
        "status": "RESOURCE-CONTROLLED CHALLENGER",
        "production": False,
        "completed": True,
        "notes": (
            "Evaluated with a 25k training cap because "
            "scikit-survival gradient boosting was substantially "
            "slower on the development laptop. Not a strict "
            "full-data apples-to-apples comparison."
        ),
    },
    {
        "rank": 6,
        "model": "Logistic Discrete-Time Hazard",
        "slug": "logistic_hazard",
        "family": "Discrete-time survival",
        "training": "Full temporal folds",
        "training_cap": None,
        "headline_auc": 0.7348428468063845,
        "worst_auc": 0.6129348424291533,
        "brier": 0.20905191849791632,
        "ece": 0.20906158752319454,
        "auc_std": 0.03769592383401725,
        "status": "SIMPLE BASELINE",
        "production": False,
        "completed": True,
        "notes": (
            "Simple hazard baseline used to determine whether "
            "nonlinear tree interactions materially improved the "
            "survival model."
        ),
    },
    {
        "rank": None,
        "model": "Random Survival Forest",
        "slug": "random_survival_forest",
        "family": "Tree survival ensemble",
        "training": "Full fold attempted",
        "training_cap": None,
        "headline_auc": None,
        "worst_auc": None,
        "brier": None,
        "ece": None,
        "auc_std": None,
        "status": "RESOURCE CONSTRAINED",
        "production": False,
        "completed": False,
        "notes": (
            "Full-data run was attempted but macOS killed the "
            "process because of memory pressure. No performance "
            "number is reported because the benchmark did not "
            "complete."
        ),
    },
]


EXPERIMENTS = [
    {
        "model": "CoxNet Elastic-Net",
        "experiment": "50k-per-fold",
        "auc": 0.8585,
        "worst_auc": 0.7430,
        "brier": 0.0973,
        "purpose": (
            "Laptop-friendly benchmark before the full-data run."
        ),
    },
    {
        "model": "CoxNet Elastic-Net",
        "experiment": "Full temporal data",
        "auc": 0.8571,
        "worst_auc": 0.7401,
        "brier": 0.0977,
        "purpose": (
            "Final CoxNet comparison."
        ),
    },
    {
        "model": "XGBoost AFT",
        "experiment": "10k / 20 rounds",
        "auc": 0.5580,
        "worst_auc": 0.2664,
        "brier": 0.3862,
        "purpose": (
            "Engineering smoke test only; deliberately "
            "undertrained."
        ),
    },
    {
        "model": "XGBoost AFT",
        "experiment": "10k / 200 rounds",
        "auc": 0.7696,
        "worst_auc": 0.5100,
        "brier": 0.2437,
        "purpose": (
            "Development run confirming that the AFT model "
            "needed substantially more boosting rounds."
        ),
    },
    {
        "model": "XGBoost AFT",
        "experiment": "Full temporal data / 200 rounds",
        "auc": 0.9006,
        "worst_auc": 0.8504,
        "brier": 0.1151,
        "purpose": (
            "Final AFT challenger benchmark."
        ),
    },
]


DATASET = {
    "dataset": "BPI Challenge 2019",
    "raw_events": 1_595_923,
    "raw_cases": 251_734,
    "eligible_invoice_cases": 199_985,
    "observed_clears": 172_385,
    "right_censored": 27_600,
    "removed_clear_before_invoice": 4,
    "prediction_point": "Record Invoice Receipt",
    "evaluation_horizons_days": [
        7,
        15,
        30,
        45,
        60,
        90,
    ],
    "serving_horizons_days": [
        7,
        30,
        60,
        90,
    ],
}


TEMPORAL_FOLDS = [
    {
        "fold": "fold_0",
        "train_cutoff": "2018-07-01",
        "eval_start": "2018-07-01",
        "eval_end": "2018-09-01",
        "train_invoices": 87_253,
        "eval_invoices": 36_905,
    },
    {
        "fold": "fold_1",
        "train_cutoff": "2018-09-01",
        "eval_start": "2018-09-01",
        "eval_end": "2018-11-01",
        "train_invoices": 124_158,
        "eval_invoices": 34_985,
    },
    {
        "fold": "fold_2",
        "train_cutoff": "2018-11-01",
        "eval_start": "2018-11-01",
        "eval_end": "2018-12-01",
        "train_invoices": 159_143,
        "eval_invoices": 15_588,
    },
    {
        "fold": "fold_3",
        "train_cutoff": "2018-12-01",
        "eval_start": "2018-12-01",
        "eval_end": "2019-01-18",
        "train_invoices": 174_731,
        "eval_invoices": 25_253,
    },
]


def fmt(value):
    if value is None:
        return "—"

    return f"{value:.4f}"


def build_markdown():
    lines = [
        "# BPI 2019 Survival Model Comparison",
        "",
        "## Dataset",
        "",
        f"- Raw events: **{DATASET['raw_events']:,}**",
        f"- Raw cases: **{DATASET['raw_cases']:,}**",
        (
            "- Eligible invoice cases: "
            f"**{DATASET['eligible_invoice_cases']:,}**"
        ),
        (
            "- Observed clears: "
            f"**{DATASET['observed_clears']:,}**"
        ),
        (
            "- Right-censored invoices: "
            f"**{DATASET['right_censored']:,}**"
        ),
        (
            "- Prediction point: "
            f"**{DATASET['prediction_point']}**"
        ),
        "",
        "## Final comparison",
        "",
        (
            "| Model | Training | AUC | Worst AUC | "
            "Brier | Status |"
        ),
        (
            "|---|---|---:|---:|---:|---|"
        ),
    ]

    for model in MODELS:
        lines.append(
            "| "
            + model["model"]
            + " | "
            + model["training"]
            + " | "
            + fmt(
                model["headline_auc"]
            )
            + " | "
            + fmt(
                model["worst_auc"]
            )
            + " | "
            + fmt(
                model["brier"]
            )
            + " | "
            + model["status"]
            + " |"
        )

    lines.extend(
        [
            "",
            "## Production selection",
            "",
            (
                "**Winner: LightGBM Discrete-Time Hazard**"
            ),
            "",
            (
                "The LightGBM hazard model remains the production "
                "champion because it achieved the best combination "
                "of discrimination, worst-case temporal stability "
                "and probability calibration."
            ),
            "",
            (
                "XGBoost AFT was the strongest challenger and "
                "approached the production model in ranking "
                "performance, but its Brier score and several "
                "horizon-level calibration errors were worse."
            ),
            "",
            "## Temporal validation",
            "",
            (
                "All full-data models use expanding temporal "
                "backtests rather than random train/test splits."
            ),
            "",
            (
                "| Fold | Train cutoff | Evaluation window | "
                "Train invoices | Evaluation invoices |"
            ),
            (
                "|---|---|---|---:|---:|"
            ),
        ]
    )

    for fold in TEMPORAL_FOLDS:
        lines.append(
            "| "
            + fold["fold"]
            + " | "
            + fold["train_cutoff"]
            + " | "
            + fold["eval_start"]
            + " → "
            + fold["eval_end"]
            + " | "
            + f"{fold['train_invoices']:,}"
            + " | "
            + f"{fold['eval_invoices']:,}"
            + " |"
        )

    lines.extend(
        [
            "",
            "## Experiment history",
            "",
            (
                "| Model | Experiment | AUC | Worst AUC | "
                "Brier | Purpose |"
            ),
            (
                "|---|---|---:|---:|---:|---|"
            ),
        ]
    )

    for experiment in EXPERIMENTS:
        lines.append(
            "| "
            + experiment["model"]
            + " | "
            + experiment["experiment"]
            + " | "
            + fmt(
                experiment["auc"]
            )
            + " | "
            + fmt(
                experiment["worst_auc"]
            )
            + " | "
            + fmt(
                experiment["brier"]
            )
            + " | "
            + experiment["purpose"]
            + " |"
        )

    lines.extend(
        [
            "",
            "## Methodology",
            "",
            (
                "- Historical training invoices are re-censored at "
                "each temporal cutoff so future clearances are not "
                "revealed to the model."
            ),
            (
                "- Right-censored unresolved invoices are retained "
                "rather than discarded."
            ),
            (
                "- AUC measures discrimination/ranking at each "
                "clearance horizon."
            ),
            (
                "- Brier score measures probability error at the "
                "fixed evaluation horizons."
            ),
            (
                "- Current Brier metrics are horizon-specific "
                "known-outcome metrics; they are not claimed as a "
                "formal IPCW integrated Brier score."
            ),
            (
                "- Gradient Boosting Survival used a 25k-per-fold "
                "resource cap and therefore is not presented as a "
                "strict full-data algorithmic comparison."
            ),
            (
                "- Random Survival Forest is listed without a score "
                "because the full benchmark did not complete under "
                "the available laptop memory."
            ),
            "",
            "## Current production outputs",
            "",
            "- `clearance_probability_7d`",
            "- `clearance_probability_30d`",
            "- `clearance_probability_60d`",
            "- `clearance_probability_90d`",
            "- `likely_clearance_window_days`",
            "- advanced `recovery_curve`",
            "",
        ]
    )

    return "\n".join(
        lines
    )


def main():
    ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "dataset": DATASET,
        "temporal_validation": {
            "strategy": (
                "expanding temporal backtest "
                "with fold-specific right re-censoring"
            ),
            "folds": TEMPORAL_FOLDS,
        },
        "production_champion":
            "lightgbm_hazard",
        "models": MODELS,
        "experiment_history": EXPERIMENTS,
        "production_model_changed":
            False,
    }

    JSON_PATH.write_text(
        json.dumps(
            payload,
            indent=2,
        )
        + "\n"
    )

    table = pd.DataFrame(
        MODELS
    )

    table.to_csv(
        CSV_PATH,
        index=False,
    )

    MD_PATH.write_text(
        build_markdown()
    )

    print()
    print(
        "=" * 90
    )
    print(
        "BPI SURVIVAL MODEL COMPARISON"
    )
    print(
        "=" * 90
    )

    display = table[
        [
            "model",
            "training",
            "headline_auc",
            "worst_auc",
            "brier",
            "status",
        ]
    ].copy()

    print(
        display.to_string(
            index=False,
        )
    )

    print()
    print(
        "Production champion:"
    )
    print(
        "  LightGBM Discrete-Time Hazard"
    )

    print()
    print(
        "Artifacts:"
    )
    print(
        " ",
        JSON_PATH,
    )
    print(
        " ",
        CSV_PATH,
    )
    print(
        " ",
        MD_PATH,
    )

    print()
    print(
        "No models were retrained."
    )
    print(
        "Production model changed: NO"
    )


if __name__ == "__main__":
    main()
