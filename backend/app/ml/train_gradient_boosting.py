"""Gradient boosting late-payment model (scikit-learn).

Run with:  python -m app.ml.train_gradient_boosting
Output:    app/ml/artifacts/models/gradient_boosting/

Shares app/ml/common.py with the XGBoost and random-forest trainers, so
the three reports are directly comparable. See that module for why the
headline metric is within-period AUC.

WHY KEEP THIS AT ALL
--------------------
It is the honest control for the XGBoost result. Both are gradient-boosted
trees on the same objective, so a large gap between them is not evidence
about boosting as a technique — it is evidence about the implementation
differences, chiefly native categorical and NaN handling, which this
estimator lacks. Having the comparison in the repo is what makes
"XGBoost wins" a measurement rather than a preference.

SAME HANDICAP AS RANDOM FOREST
------------------------------
sklearn's GradientBoostingClassifier has no native categorical or NaN
support, so this trains on integer-coded categoricals with NaNs
median-filled from TRAIN, and the medians are saved beside the model. The
saved estimator is unusable without them: scoring a raw frame against it
produces wrong answers silently rather than raising.

A note on speed: this implementation is single-threaded and fits one tree
at a time, so the candidate grid here is smaller than the XGBoost one on
purpose. That is a cost difference worth knowing before anyone proposes
this for production retraining.
"""

from __future__ import annotations

import json

import joblib
import sklearn
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from app.ml.common import (
    ARTIFACT_ROOT,
    FEATURES,
    MIN_HISTORY_FOR_SCORING,
    RANDOM_STATE,
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
from app.ml.datasets.build_primary_features import (
    MODEL_CATEGORICAL_FEATURES,
    MODEL_NUMERIC_FEATURES,
    TARGET_COLUMN,
)

MODEL_SLUG = "gradient_boosting"

CANDIDATES = [
    dict(n_estimators=300, learning_rate=0.05, max_depth=3, min_samples_leaf=40, subsample=0.85),
    dict(n_estimators=300, learning_rate=0.05, max_depth=4, min_samples_leaf=40, subsample=0.85),
    dict(n_estimators=500, learning_rate=0.03, max_depth=4, min_samples_leaf=20, subsample=0.85),
    dict(n_estimators=500, learning_rate=0.03, max_depth=5, min_samples_leaf=40, subsample=0.90),
]

FIXED = dict(random_state=RANDOM_STATE)


def main() -> dict:
    train, validation, test = load_splits()
    X_train_cat, X_validation_cat, X_test_cat = prepare_categorical(
        [train, validation, test]
    )
    (X_train, X_validation, _), medians = to_numeric_codes(
        [X_train_cat, X_validation_cat, X_test_cat]
    )
    print_data_summary(train, validation, test, "Gradient Boosting")
    print(f"  encoding   integer codes + median impute ({len(medians)} columns)")

    print()
    print("=" * 78)
    print("CANDIDATE SELECTION  (on validation, within-period AUC)")
    print("=" * 78)
    print(f"  {'#':>2}  {'trees':>6} {'lr':>5} {'depth':>6} {'leaf':>5} {'sub':>5}"
          f"  {'within':>7} {'pooled':>7} {'logloss':>8}")

    results = []
    for index, candidate in enumerate(CANDIDATES, start=1):
        model = GradientBoostingClassifier(**candidate, **FIXED)
        model.fit(X_train, train[TARGET_COLUMN])
        scored = evaluate(validation, model.predict_proba(X_validation)[:, 1])
        results.append((candidate, scored, model))
        print(f"  {index:>2}  {candidate['n_estimators']:>6} "
              f"{candidate['learning_rate']:>5} {candidate['max_depth']:>6} "
              f"{candidate['min_samples_leaf']:>5} {candidate['subsample']:>5}"
              f"  {scored['within_period_auc']:>7.4f} "
              f"{scored['pooled_roc_auc']:>7.4f} {scored['log_loss']:>8.4f}")

    best_config, best_scores, best_model = max(
        results, key=lambda row: row[1]["within_period_auc"]
    )
    print(f"\n  selected: {best_config}")

    print()
    print("=" * 78)
    print(f"TEMPORAL FOLDS  (rows with >= {MIN_HISTORY_FOR_SCORING} resolved prior invoices)")
    print("=" * 78)
    folds = temporal_folds(train)
    fold_aucs = []
    for number, (train_idx, test_idx) in enumerate(folds):
        fold_model = GradientBoostingClassifier(**best_config, **FIXED)
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
    print_comparison_row("gradient_boosting (this model)", best_scores)
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
    for name, value in list(importances.items())[:10]:
        print(f"    {name:<36} {value:>9.4f}")

    directory = ARTIFACT_ROOT / MODEL_SLUG
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.joblib"
    joblib.dump(best_model, model_path)
    (directory / "median_impute_values.json").write_text(
        json.dumps({k: float(v) for k, v in medians.items()}, indent=2) + "\n"
    )

    train_scores = evaluate(train, best_model.predict_proba(X_train)[:, 1])
    report = {
        "model": "gradient_boosting",
        "library": f"scikit-learn=={sklearn.__version__}",
        "target": TARGET_COLUMN,
        "test_used": False,
        "selection_metric": "within_period_auc on validation",
        "selected_configuration": {**best_config, **FIXED},
        "features": {
            "order": FEATURES,
            "numeric": MODEL_NUMERIC_FEATURES,
            "categorical": MODEL_CATEGORICAL_FEATURES,
        },
        "preprocessing": {
            "categorical_handling": "integer codes via pandas .cat.codes",
            "nan_handling": "median-imputed from train",
            "median_values_file": "median_impute_values.json",
            "note": (
                "Same handicap as random_forest: no native categorical or NaN "
                "support, so the gap to XGBoost is partly an encoding artefact "
                "rather than purely a model-family difference."
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
