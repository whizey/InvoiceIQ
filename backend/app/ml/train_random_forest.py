"""Random forest late-payment model.

Run with:  python -m app.ml.train_random_forest
Output:    app/ml/artifacts/models/random_forest/

Shares app/ml/common.py with the XGBoost and gradient-boosting trainers,
so the three reports are directly comparable. See that module for why the
headline metric is within-period AUC.

HANDICAP, STATED UP FRONT
-------------------------
scikit-learn's RandomForestClassifier has neither native categorical
support nor native NaN handling, so this model is trained on integer-coded
categoricals with NaNs median-filled from TRAIN. XGBoost gets the raw
frame. That is not a like-for-like contest, and the gap between the two
is therefore partly a feature-encoding artefact rather than purely a
model-family difference.

Two consequences worth knowing:

  - Integer codes impose a fake ordering on city, region and zipcode. A
    tree can still carve them up, but it needs more splits to do what
    XGBoost does in one, which costs depth budget.
  - The NaNs are real signal, not noise: historical_late_rate is NaN
    exactly when a customer has no resolved history (about 5.7% of rows),
    and has_resolved_history flags that. Median-filling makes those rows
    look like average customers on the rate features while the flag still
    says otherwise, so the model has to learn the interaction instead of
    being told.

The median values are saved next to the model as median_impute_values.json
because the saved estimator is unusable without them — scoring a raw frame
against it produces wrong answers silently rather than raising.
"""

from __future__ import annotations

import json

import joblib
import sklearn
from sklearn.ensemble import RandomForestClassifier
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
    to_numeric_codes,
    write_report,
)
from app.ml.selection_policy import PRIMARY_SELECTION_METRIC
from app.ml.datasets.build_primary_features import (
    MODEL_CATEGORICAL_FEATURES,
    MODEL_NUMERIC_FEATURES,
    TARGET_COLUMN,
)

MODEL_SLUG = "random_forest"

CANDIDATES = [
    dict(n_estimators=600, max_depth=10, min_samples_leaf=40, max_features=0.5),
    dict(n_estimators=600, max_depth=12, min_samples_leaf=40, max_features=0.5),
    dict(n_estimators=600, max_depth=16, min_samples_leaf=20, max_features=0.5),
    dict(n_estimators=900, max_depth=12, min_samples_leaf=20, max_features="sqrt"),
    dict(n_estimators=900, max_depth=None, min_samples_leaf=60, max_features=0.5),
]

FIXED = dict(random_state=RANDOM_STATE, n_jobs=-1)


def main() -> dict:
    train, validation, test = load_splits()
    category_levels = category_levels_from_train(train)
    X_train_cat, X_validation_cat, X_test_cat = prepare_categorical(
        [train, validation, test]
    )
    (X_train, X_validation, _), medians = to_numeric_codes(
        [X_train_cat, X_validation_cat, X_test_cat]
    )
    print_data_summary(train, validation, test, "Random Forest")
    print(f"  encoding   integer codes + median impute ({len(medians)} columns)")

    print()
    print("=" * 78)
    print("CANDIDATE SELECTION  (on validation, within-period AUC)")
    print("=" * 78)
    print(f"  {'#':>2}  {'trees':>6} {'depth':>6} {'leaf':>5} {'feats':>7}"
          f"  {'within':>7} {'pooled':>7} {'logloss':>8}")

    results = []
    for index, candidate in enumerate(CANDIDATES, start=1):
        model = RandomForestClassifier(**candidate, **FIXED)
        model.fit(X_train, train[TARGET_COLUMN])
        scored = evaluate(validation, model.predict_proba(X_validation)[:, 1])
        results.append((candidate, scored, model))
        print(f"  {index:>2}  {candidate['n_estimators']:>6} "
              f"{str(candidate['max_depth']):>6} {candidate['min_samples_leaf']:>5} "
              f"{str(candidate['max_features']):>7}  {scored['within_period_auc']:>7.4f} "
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
        fold_train_frame = train.iloc[train_idx]
        fold_frame = train.iloc[test_idx]

        fold_train_cat, fold_test_cat = prepare_categorical(
            [fold_train_frame, fold_frame]
        )

        (
            fold_X_train,
            fold_X_test,
        ), _fold_medians = to_numeric_codes(
            [fold_train_cat, fold_test_cat]
        )

        fold_model = RandomForestClassifier(**best_config, **FIXED)
        fold_model.fit(
            fold_X_train,
            fold_train_frame[TARGET_COLUMN],
        )

        probabilities = fold_model.predict_proba(
            fold_X_test
        )[:, 1]
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
    print_comparison_row("random_forest (this model)", best_scores)
    base = baselines(train, validation)
    for name, scored in base.items():
        print_comparison_row(f"baseline: {name}", scored)

    print()
    print("  BY MONTH")
    for month, stats in sorted(best_scores["per_month"].items()):
        print(f"    {month}  rows {stats['rows']:>5}  late {stats['late_rate']:.4f}"
              f"  auc {stats['auc']:.4f}")

    importances = dict(
        sorted(
            zip(FEATURES, (float(v) for v in best_model.feature_importances_)),
            key=lambda kv: -kv[1],
        )
    )
    print()
    print("  TOP FEATURES BY IMPURITY DECREASE")
    print("  (impurity importance can favor variables offering more candidate")
    print("   split points; treat this as a model diagnostic, not causal importance)")
    for name, value in list(importances.items())[:10]:
        print(f"    {name:<36} {value:>9.4f}")

    directory = ARTIFACT_ROOT / MODEL_SLUG
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.joblib"
    joblib.dump(best_model, model_path)
    median_path = directory / "median_impute_values.json"
    median_path.write_text(
        json.dumps({k: float(v) for k, v in medians.items()}, indent=2) + "\n"
    )

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
            median_path=median_path,
        )
    )

    train_scores = evaluate(train, best_model.predict_proba(X_train)[:, 1])
    report = {
        "model": "random_forest",
        "library": f"scikit-learn=={sklearn.__version__}",
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
            "nan_handling": "median-imputed from train",
            "median_values_file": "median_impute_values.json",
            "note": (
                "The saved estimator is unusable without these medians and the "
                "exact feature order above: scoring a raw frame produces wrong "
                "answers silently rather than raising. This encoding is a "
                "handicap relative to XGBoost, which handles both natively."
            ),
        },
        "candidates": [
            {**{k: str(v) for k, v in config.items()},
             **{k: scored[k] for k in ("within_period_auc", "pooled_roc_auc", "log_loss")}}
            for config, scored, _ in results
        ],
        "train": train_scores,
        "validation": best_scores,
        "train_validation_within_period_gap": round(
            train_scores["within_period_auc"] - best_scores["within_period_auc"], 4
        ),
        "temporal_folds": {"min_history_for_scoring": MIN_HISTORY_FOR_SCORING, **stability},
        "baselines": base,
        "feature_importance_impurity": {k: round(v, 4) for k, v in importances.items()},
        "feature_importance_caveat": (
            "Impurity-based importance is biased toward high-cardinality "
            "features, so city and zipcode are overstated here. Not comparable "
            "with the XGBoost report's gain values."
        ),
        "headline_caveat": None if stability.get("stable") else (
            "Validation within-period AUC is NOT this model's expected performance. "
            f"Temporal folds span {stability['min']:.4f}-{stability['max']:.4f} and "
            f"{len(stability['inverted_folds'])} invert below 0.50."
        ),
    }
    path = write_report(MODEL_SLUG, report)
    print()
    print(f"  model  -> {model_path}")
    print(f"  medians-> {directory / 'median_impute_values.json'}")
    print(f"  report -> {path}")
    print()
    print("  TEST SPLIT WAS NOT READ.")
    return report


if __name__ == "__main__":
    main()
