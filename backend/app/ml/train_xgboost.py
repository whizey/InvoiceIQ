"""XGBoost late-payment model.

Run with:  python -m app.ml.train_xgboost
Output:    app/ml/artifacts/models/xgboost/

Data loading, metrics, folds and the stability check come from
app/ml/common.py so this file and its two siblings produce numbers that
are actually comparable. See that module for why the headline metric is
within-period AUC rather than pooled ROC-AUC.

Selection is gated on VALIDATION, not on cross-validation alone. The
previous version scored candidates with

    mean_cv_auc - 0.50 * std_cv_auc - 0.25 * (mean_cv_auc - min_cv_auc)

which rewards agreement across folds. That sounds right and was actively
harmful: the causal history features need accumulated history, so the
earliest fold scores near chance, and penalising the minimum therefore
penalised exactly the models that used history well. The resulting
ranking came out anti-correlated with validation — the model previously
named "champion" was beaten on validation by the plain baseline. Folds
are still computed here, but for the stability verdict, not for choosing.

XGBoost is the only one of the three with native categorical and native
NaN support, so it trains on the frame as-is. That matters for a fair
comparison: the sklearn pair have to be given integer codes and
median-filled NaNs, which is a handicap, and their reports say so.
"""

from __future__ import annotations


import xgboost as xgb
from sklearn.metrics import roc_auc_score

from app.ml.artifact_contract import save_training_contract
from app.ml.common import (
    ARTIFACT_ROOT,
    FEATURES,
    MIN_HISTORY_FOR_SCORING,
    RANDOM_STATE,
    SPLIT_DIR,
    category_levels_from_train,
    baselines,
    evaluate,
    fold_base_rates,
    load_splits,
    prepare_categorical,
    print_comparison_row,
    print_comparison_row_header,
    print_data_summary,
    print_stability,
    stability_report,
    temporal_folds,
    write_report,
)
from app.ml.selection_policy import PRIMARY_SELECTION_METRIC
from app.ml.datasets.build_primary_features import (
    MODEL_CATEGORICAL_FEATURES,
    MODEL_NUMERIC_FEATURES,
    TARGET_COLUMN,
)

MODEL_SLUG = "xgboost"

# Deliberately small and legible. The scripts this replaced searched 24-67
# configurations and selected on a metric anti-correlated with validation,
# so search size was never the limiting factor — the objective was.
CANDIDATES = [
    dict(max_depth=3, learning_rate=0.05, n_estimators=400, min_child_weight=20, reg_lambda=5.0),
    dict(max_depth=4, learning_rate=0.05, n_estimators=400, min_child_weight=20, reg_lambda=5.0),
    dict(max_depth=4, learning_rate=0.03, n_estimators=600, min_child_weight=10, reg_lambda=5.0),
    dict(max_depth=5, learning_rate=0.03, n_estimators=600, min_child_weight=20, reg_lambda=10.0),
    dict(max_depth=5, learning_rate=0.05, n_estimators=400, min_child_weight=5, reg_lambda=2.0),
    dict(max_depth=6, learning_rate=0.03, n_estimators=600, min_child_weight=20, reg_lambda=10.0),
]

FIXED = dict(
    subsample=0.85,
    colsample_bytree=0.85,
    eval_metric="logloss",
    random_state=RANDOM_STATE,
    tree_method="hist",
    enable_categorical=True,
)


def main() -> dict:
    train, validation, test = load_splits()
    category_levels = category_levels_from_train(train)
    X_train, X_validation, _ = prepare_categorical([train, validation, test])
    print_data_summary(train, validation, test, "XGBoost")

    print()
    print("=" * 78)
    print("CANDIDATE SELECTION  (on validation, within-period AUC)")
    print("=" * 78)
    print(f"  {'#':>2}  {'depth':>5} {'lr':>5} {'trees':>6} {'mcw':>4} {'lambda':>7}"
          f"  {'within':>7} {'pooled':>7} {'logloss':>8}")

    results = []
    for index, candidate in enumerate(CANDIDATES, start=1):
        model = xgb.XGBClassifier(**candidate, **FIXED)
        model.fit(X_train, train[TARGET_COLUMN])
        scored = evaluate(validation, model.predict_proba(X_validation)[:, 1])
        results.append((candidate, scored, model))
        print(f"  {index:>2}  {candidate['max_depth']:>5} {candidate['learning_rate']:>5} "
              f"{candidate['n_estimators']:>6} {candidate['min_child_weight']:>4} "
              f"{candidate['reg_lambda']:>7}  {scored['within_period_auc']:>7.4f} "
              f"{scored['pooled_roc_auc']:>7.4f} {scored['log_loss']:>8.4f}")

    best_config, best_scores, best_model = max(
        results, key=lambda row: row[1][PRIMARY_SELECTION_METRIC]
    )
    print(f"\n  selected: {best_config}")

    print()
    print("=" * 78)
    print(f"TEMPORAL FOLDS  (rows with >= {MIN_HISTORY_FOR_SCORING} resolved prior invoices)")
    print("=" * 78)
    folds = temporal_folds(train)
    fold_aucs = []
    for number, (train_idx, test_idx) in enumerate(folds):
        fold_model = xgb.XGBClassifier(**best_config, **FIXED)
        fold_model.fit(X_train.iloc[train_idx], train[TARGET_COLUMN].iloc[train_idx])
        fold_frame = train.iloc[test_idx]
        probabilities = fold_model.predict_proba(X_train.iloc[test_idx])[:, 1]
        eligible = (
            fold_frame["prior_resolved_invoice_count"] >= MIN_HISTORY_FOR_SCORING
        ).to_numpy()
        if eligible.sum() < 50 or fold_frame.loc[eligible, TARGET_COLUMN].nunique() < 2:
            print(f"  fold {number}: skipped, too few eligible rows")
            continue
        auc = float(
            roc_auc_score(fold_frame.loc[eligible, TARGET_COLUMN], probabilities[eligible])
        )
        fold_aucs.append(auc)
        print(f"  fold {number}: train {len(train_idx):>6}  eligible {int(eligible.sum()):>5}"
              f"  auc {auc:.4f}")

    stability = stability_report(fold_aucs, fold_base_rates(train, folds))
    print_stability(stability)

    print()
    print("=" * 78)
    print("VALIDATION  vs  BASELINES")
    print("=" * 78)
    print_comparison_row_header()
    print_comparison_row("xgboost (this model)", best_scores)
    base = baselines(train, validation)
    for name, scored in base.items():
        print_comparison_row(f"baseline: {name}", scored)

    print()
    print("  BY MONTH")
    for month, stats in sorted(best_scores["per_month"].items()):
        print(f"    {month}  rows {stats['rows']:>5}  late {stats['late_rate']:.4f}"
              f"  auc {stats['auc']:.4f}")

    importance = best_model.get_booster().get_score(importance_type="gain")
    print()
    print("  TOP FEATURES BY GAIN")
    for name, value in sorted(importance.items(), key=lambda kv: -kv[1])[:10]:
        print(f"    {name:<36} {value:>9.1f}")

    directory = ARTIFACT_ROOT / MODEL_SLUG
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.json"
    best_model.save_model(str(model_path))

    _training_contract_path = (
        save_training_contract(
            model_name=MODEL_SLUG,
            directory=directory,
            model_path=model_path,
            train_split_path=(
                SPLIT_DIR
                / "train.csv.gz"
            ),
            category_levels=(
                category_levels
            ),
        )
    )

    train_scores = evaluate(train, best_model.predict_proba(X_train)[:, 1])
    report = {
        "model": "xgboost",
        "library": f"xgboost=={xgb.__version__}",
        "target": TARGET_COLUMN,
        "test_used": False,
        "selection_metric": f"{PRIMARY_SELECTION_METRIC} on validation",
        "selected_configuration": {**best_config, **FIXED},
        "features": {
            "order": FEATURES,
            "numeric": MODEL_NUMERIC_FEATURES,
            "categorical": MODEL_CATEGORICAL_FEATURES,
        },
        "preprocessing": {
            "categorical_handling": "none (0 categorical features)",
            "nan_handling": "native",
            "note": "Final contract is numeric-only; XGBoost handles NaNs natively.",
        },
        "candidates": [
            {**config, **{k: scored[k] for k in
                          ("within_period_auc", "pooled_roc_auc", "log_loss")}}
            for config, scored, _ in results
        ],
        "train": train_scores,
        "validation": best_scores,
        "train_validation_within_period_gap": round(
            train_scores["within_period_auc"] - best_scores["within_period_auc"], 4
        ),
        "temporal_folds": {"min_history_for_scoring": MIN_HISTORY_FOR_SCORING, **stability},
        "baselines": base,
        "feature_importance_gain": {
            k: round(float(v), 2)
            for k, v in sorted(importance.items(), key=lambda kv: -kv[1])
        },
        "headline_caveat": None if stability.get("stable") else (
            "Validation within-period AUC is NOT this model's expected performance. "
            f"Temporal folds span {stability['min']:.4f}-{stability['max']:.4f} and "
            f"{len(stability['inverted_folds'])} invert below 0.50, because the late "
            "rate changes regime between adjacent five-week windows."
        ),
    }
    path = write_report(MODEL_SLUG, report)
    print()
    print(f"  model  -> {model_path}")
    print(f"  report -> {path}")
    print()
    print("  TEST SPLIT WAS NOT READ.")
    return report


if __name__ == "__main__":
    main()
