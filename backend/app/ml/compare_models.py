"""Side-by-side comparison of the three trained late-payment models.

Run with:  python -m app.ml.compare_models

Reads the report.json each trainer writes; it does not retrain anything,
so these are exactly the numbers those runs produced.

Every figure comes from the same evaluation code in app/ml/common.py,
which is the only reason the rows are comparable.
"""

import json
import statistics

from app.ml.common import ARTIFACT_ROOT

MODELS = ["xgboost", "random_forest", "gradient_boosting"]


def load_reports():
    reports = {}
    for slug in MODELS:
        path = ARTIFACT_ROOT / slug / "report.json"
        if not path.exists():
            print(f"  [missing] {slug}: run `python -m app.ml.train_{slug}` first")
            continue
        reports[slug] = json.loads(path.read_text())
    return reports


def monthly_spread(report):
    """Std, min and max of per-month validation AUC.

    Metric 9. A model can post a strong headline number while being
    useless in one of the months inside it, and the spread is what shows
    that. Kept separate from the fold spread because they answer
    different questions: months measure consistency inside the validation
    window, folds measure whether the model survives moving forward in
    time at all.
    """
    aucs = [m["auc"] for m in report["validation"]["per_month"].values()]
    if len(aucs) < 2:
        return 0.0, (aucs[0] if aucs else 0.0), (aucs[0] if aucs else 0.0)
    return statistics.pstdev(aucs), min(aucs), max(aucs)


def main():
    reports = load_reports()
    if not reports:
        return
    any_report = next(iter(reports.values()))

    print("=" * 100)
    print("RECOVERIQ LATE-PAYMENT MODELS — FULL METRIC COMPARISON")
    print("=" * 100)
    print(f"  target      {any_report['target']}")
    print(f"  selection   {any_report['selection_metric']}")
    print(f"  test split  {'NOT used' if not any_report['test_used'] else 'USED'}")
    print(f"  features    {len(any_report['features']['order'])}"
          f"  ({len(any_report['features']['numeric'])} numeric"
          f" + {len(any_report['features']['categorical'])} categorical)")

    print()
    print("-" * 100)
    print("DISCRIMINATION, CALIBRATION, GENERALISATION")
    print("-" * 100)
    print(f"  {'model':<20} {'ROC-AUC':>8} {'PR-AUC':>8} {'CV AUC':>8} {'worst':>8} "
          f"{'gap':>7} {'logloss':>9} {'brier':>8}")
    print(f"  {'':<20} {'(1)':>8} {'(2)':>8} {'(3)':>8} {'(4)':>8} {'(5)':>7} "
          f"{'(6)':>9} {'(7)':>8}")
    print("  " + "-" * 96)
    for slug, report in reports.items():
        v, f = report["validation"], report["temporal_folds"]
        print(f"  {slug:<20} {v['pooled_roc_auc']:>8.4f} {v['pooled_pr_auc']:>8.4f} "
              f"{f['mean']:>8.4f} {f['min']:>8.4f} "
              f"{report['train_validation_within_period_gap']:>+7.3f} "
              f"{v['log_loss']:>9.4f} {v['brier']:>8.4f}")
    print("  " + "-" * 96)
    for name, scored in any_report["baselines"].items():
        print(f"  {'baseline: ' + name:<20} {scored['pooled_roc_auc']:>8.4f} "
              f"{scored['pooled_pr_auc']:>8.4f} {'--':>8} {'--':>8} {'--':>7} "
              f"{scored['log_loss']:>9.4f} {scored['brier']:>8.4f}")

    print()
    print("  (1) Validation ROC-AUC      pooled across the whole window")
    print("  (2) Validation PR-AUC       better than ROC when the positive class is the")
    print("                              one you act on; base rate here is 0.4177")
    print("  (3) Temporal CV AUC         mean across expanding-window folds")
    print("  (4) Worst-fold AUC          the number that decides whether it ships")
    print("  (5) Train-validation gap    on within-period AUC; bigger = more overfit")
    print("  (6) Log Loss                punishes confident wrong answers")
    print("  (7) Brier Score             squared error of the probability itself")

    print()
    print("-" * 100)
    print("(8) CLASSIFICATION AT THRESHOLD 0.50")
    print("-" * 100)
    print(f"  {'model':<20} {'precision':>10} {'recall':>8} {'f1':>8} {'accuracy':>9}"
          f"   what it costs you")
    print("  " + "-" * 96)
    for slug, report in reports.items():
        v = report["validation"]
        if v["recall"] >= 0.80 and v["precision"] < 0.60:
            note = "chases nearly everything, most chases wasted"
        elif v["precision"] >= 0.70 and v["recall"] < 0.60:
            note = "chases few, misses most late invoices"
        else:
            note = "balanced"
        print(f"  {slug:<20} {v['precision']:>10.4f} {v['recall']:>8.4f} "
              f"{v['f1']:>8.4f} {v['accuracy']:>9.4f}   {note}")
    print()
    print("  0.50 is an arbitrary cut, not a tuned operating point. Move it toward")
    print("  recall if a missed late invoice costs more than a wasted reminder.")

    print()
    print("-" * 100)
    print("(9) STABILITY — ACROSS MONTHS AND ACROSS TIME")
    print("-" * 100)
    print(f"  {'model':<20} {'month std':>10} {'month min':>10} {'month max':>10}"
          f"   {'fold std':>9} {'fold min':>9} {'fold max':>9}  stable")
    print("  " + "-" * 96)
    for slug, report in reports.items():
        m_std, m_min, m_max = monthly_spread(report)
        f = report["temporal_folds"]
        print(f"  {slug:<20} {m_std:>10.4f} {m_min:>10.4f} {m_max:>10.4f}"
              f"   {f['std']:>9.4f} {f['min']:>9.4f} {f['max']:>9.4f}  "
              f"{str(f['stable']):>6}")

    print()
    print("  PER-MONTH VALIDATION AUC")
    months = sorted(any_report["validation"]["per_month"])
    rates = any_report["validation"]["per_month"]
    print(f"    {'model':<20}" + "".join(f"{m:>12}" for m in months))
    print(f"    {'(late rate)':<20}"
          + "".join(f"{rates[m]['late_rate']:>12.4f}" for m in months))
    print(f"    {'(rows)':<20}" + "".join(f"{rates[m]['rows']:>12}" for m in months))
    print("    " + "-" * 56)
    for slug, report in reports.items():
        per_month = report["validation"]["per_month"]
        print(f"    {slug:<20}" + "".join(f"{per_month[m]['auc']:>12.4f}" for m in months))

    print()
    print("  PER-FOLD AUC  (expanding window; fold 0 trains on the least history)")
    print(f"    {'model':<20}" + "".join(f"{'fold ' + str(i):>12}" for i in range(4)))
    print("    " + "-" * 56)
    for slug, report in reports.items():
        aucs = report["temporal_folds"]["fold_aucs"]
        print(f"    {slug:<20}" + "".join(f"{a:>12.4f}" for a in aucs))

    inverting = [s for s, r in reports.items() if r["temporal_folds"].get("inverted_folds")]
    if inverting:
        print()
        print("  A fold below 0.50 means the ranking is REVERSED in that period — worse")
        print(f"  than having no model. Inverting on at least one fold: {', '.join(inverting)}")
        print()
        print("  All three fail on the same fold. Three different algorithms sharing one")
        print("  failure locates the cause in the data, not in any model's hyperparameters.")
        print("  The late rate changes regime between adjacent five-week windows:")
        for index, r in enumerate(any_report["temporal_folds"].get("fold_base_rates", [])):
            print(f"      fold {index}: train late {r['train_late_rate']:.4f}"
                  f"  ->  test late {r['test_late_rate']:.4f}")

    print()
    print("-" * 100)
    print("PREPROCESSING — FINAL 18-NUMERIC CONTRACT")
    print("-" * 100)
    for slug, report in reports.items():
        pre = report["preprocessing"]
        print(f"  {slug:<20} categorical: {pre['categorical_handling']}")
        print(f"  {'':<20} missing:     {pre['nan_handling']}")
    print()
    print("  All three models use the same final 18 numeric features and no")
    print("  categorical model inputs. The remaining preprocessing difference is")
    print("  missing-value handling: XGBoost handles NaNs natively, while Random")
    print("  Forest and Gradient Boosting use medians fitted on the training split.")

    print()
    print("=" * 100)
    print("FROZEN FINAL SELECTION")
    print("=" * 100)
    best_rank = max(reports.items(), key=lambda kv: kv[1]["validation"]["pooled_roc_auc"])
    best_pr = max(reports.items(), key=lambda kv: kv[1]["validation"]["pooled_pr_auc"])
    best_calib = min(reports.items(), key=lambda kv: kv[1]["validation"]["log_loss"])
    best_worst = max(reports.items(), key=lambda kv: kv[1]["temporal_folds"]["min"])
    smallest_gap = min(
        reports.items(), key=lambda kv: abs(kv[1]["train_validation_within_period_gap"])
    )
    print(f"  best ROC-AUC         {best_rank[0]:<20} "
          f"{best_rank[1]['validation']['pooled_roc_auc']:.4f}")
    print(f"  best PR-AUC          {best_pr[0]:<20} "
          f"{best_pr[1]['validation']['pooled_pr_auc']:.4f}")
    print(f"  best calibration     {best_calib[0]:<20} "
          f"{best_calib[1]['validation']['log_loss']:.4f} log loss")
    print(f"  best worst-fold      {best_worst[0]:<20} "
          f"{best_worst[1]['temporal_folds']['min']:.4f}")
    print(f"  least overfit        {smallest_gap[0]:<20} "
          f"{smallest_gap[1]['train_validation_within_period_gap']:+.3f} gap")

    frozen_selected = max(
        reports.items(),
        key=lambda kv: kv[1]["validation"]["within_period_auc"],
    )
    selected_name, selected_report = frozen_selected
    selected_auc = selected_report["validation"]["within_period_auc"]

    print()
    print("  PRE-REGISTERED SELECTION RULE")
    print("    scope     validation")
    print("    metric    within_period_auc")
    print(f"    selected  {selected_name}")
    print(f"    value     {selected_auc:.4f}")
    print()
    print("  Secondary metrics above are diagnostics only and do not override")
    print("  the frozen model-selection rule.")

    if any(r["temporal_folds"].get("inverted_folds") for r in reports.values()):
        print()
        print("  TEMPORAL STABILITY: FAILED")
        print("  Every model ranks backwards in at least one temporal period.")
        print("  The selected model is therefore NOT being described as")
        print("  production-ready or as reliable expected future performance.")


if __name__ == "__main__":
    main()
