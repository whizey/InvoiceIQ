"""Read-only Phase 4 validation of frozen artifacts and historical cohorts.

No fitting, calibration fitting, database writes, or promotion. Serialized
models are loaded only through existing hash-verified serving loaders.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score, brier_score_loss, confusion_matrix, f1_score,
    log_loss, precision_score, recall_score, roc_auc_score,
)

from app.ml.artifact_contract import sha256_file
from app.ml.drift_monitor import compare_feature
from app.ml.payment_features import FEATURES, TARGET_COLUMN, PaymentRiskFeatures

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SPLITS = PROJECT_ROOT / "data/processed/primary/splits"
ARTIFACTS = Path(__file__).parent / "artifacts"
CUTOFF = "2016-03-17"
THRESHOLDS = (0.25, 0.5, 0.75)


def classification_metrics(y, probabilities, *, thresholds=THRESHOLDS, bins=10) -> dict:
    """Fixed-bin ECE and average precision (step-integral PR-AUC).

    Undefined discrimination/precision/recall is null, never a fabricated
    zero. F1 uses zero when a present positive class has no true positives.
    """
    y = np.asarray(y)
    p = np.asarray(probabilities, dtype=float)
    if y.ndim != 1 or p.shape != y.shape or not len(y):
        raise ValueError("Metrics require nonempty aligned one-dimensional arrays")
    if not np.isin(y, [0, 1]).all() or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError("Metrics require binary labels and finite probabilities in [0, 1]")
    if isinstance(bins, bool) or not isinstance(bins, int) or not 2 <= bins <= 100:
        raise ValueError("Calibration bins must be between 2 and 100")
    y = y.astype(int)
    both_classes = len(np.unique(y)) == 2
    reliability = []
    bucket = np.minimum((p * bins).astype(int), bins - 1)
    ece = 0.0
    for index in range(bins):
        mask = bucket == index
        count = int(mask.sum())
        mean_p = float(p[mask].mean()) if count else None
        observed = float(y[mask].mean()) if count else None
        if count:
            ece += count / len(y) * abs(mean_p - observed)
        reliability.append({"lower": index / bins, "upper": (index + 1) / bins,
                            "rows": count, "mean_probability": mean_p, "observed_rate": observed})
    operational = []
    for threshold in thresholds:
        if not np.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Threshold must be in [0, 1]")
        pred = (p >= threshold).astype(int)
        tn, fp, fn, tp = (int(v) for v in confusion_matrix(y, pred, labels=[0, 1]).ravel())
        precision = float(precision_score(y, pred, zero_division=0)) if tp + fp else None
        recall = float(recall_score(y, pred, zero_division=0)) if tp + fn else None
        specificity = tn / (tn + fp) if tn + fp else None
        operational.append({"threshold": threshold, "selected_rows": tp + fp,
            "precision": precision, "recall": recall,
            "f1": float(f1_score(y, pred, zero_division=0)) if tp + fp + fn else None,
            "balanced_accuracy": (recall + specificity) / 2 if both_classes else None,
            "confusion_matrix": [[tn, fp], [fn, tp]]})
    return {"rows": len(y), "positive_rows": int(y.sum()), "prevalence": float(y.mean()),
        "roc_auc": float(roc_auc_score(y, p)) if both_classes else None,
        "pr_auc": float(average_precision_score(y, p)) if y.sum() else None,
        "pr_auc_definition": "average_precision; step integral, not trapezoidal PR area",
        "discrimination_status": "defined" if both_classes else "single_class",
        "brier_score": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "ece": ece, "ece_bins": bins, "reliability": reliability,
        "operational_thresholds": operational}


def target_contract() -> dict:
    current = {"amount", "payment_term"}
    return {"prediction_time": "invoice posting, start of day; same-day invoices share history",
        "observation_cutoff": CUTOFF,
        "targets": {
            "late_payment": "clearing_date > weekend-adjusted net_due_date",
            "delay_over_7d": "signed weekend-adjusted days late > 7",
            "delay_over_15d": "signed weekend-adjusted days late > 15",
            "days_late_if_late_mean": "positive weekend-adjusted delay, conditional on late",
            "days_late_p80": "conditional positive-delay 0.8 quantile; not a guarantee",
        },
        "label_availability": "Classification horizon must be strictly before fit cutoff; exact delay requires clearing before cutoff. These are retrospective labels, never inputs.",
        "feature_availability": {name: ("current invoice known at posting" if name in current else
            "prior postings only; outcome aggregates require clearing strictly before current posting; strict raw-due history, not weekend-adjusted target") for name in FEATURES},
        "excluded_inputs": ["customer identity", "clearing date", "current delay", "source_delayflag", "future invoices", "future payments"],
        "other_prediction_times": {"due_date": "not separately trained or validated",
            "historical_as_of": "serving uses persisted posting-time predictions, not a newly validated as-of model",
            "post_payment": "labels and realized delays are retrospective analytics"},
        "limitations": ["Completed-only extract excludes unpaid invoices; 45-day eligibility does not remove population selection bias.",
            "Cutoff is an extraction boundary, not evidence of a prospective production test.",
            "Historical payment behavior can proxy identity despite explicit customer IDs being excluded."]}


def _read_splits() -> dict[str, pd.DataFrame]:
    frames = {name: pd.read_csv(SPLITS / f"{name}.csv.gz", dtype={"document_no": str, "cust_num": str},
              parse_dates=["posting_date", "net_due_date", "doc_date"]) for name in ("train", "validation", "test")}
    holdout = json.loads((SPLITS.parent / "customer_holdout_manifest.json").read_text())
    for name in ("train", "validation"):
        expected = holdout["source_files"][name + ".csv.gz"] if "source_files" in holdout else None
        # The manifest is inventoried below; known split fingerprints are
        # checked through the deployed models' training contracts separately.
        if expected and isinstance(expected, str) and sha256_file(SPLITS / f"{name}.csv.gz") != expected:
            raise ValueError("Frozen development split fingerprint mismatch")
    prior = set()
    for name, frame in frames.items():
        ids = set(frame.document_no)
        if frame.document_no.duplicated().any() or prior.intersection(ids):
            raise ValueError("Duplicate invoice contamination")
        prior.update(ids)
        if not np.isin(frame[TARGET_COLUMN], [0, 1]).all():
            raise ValueError("Invalid frozen labels")
        if name != "train":
            preceding = frames["train" if name == "validation" else "validation"]
            if preceding.posting_date.max() >= frame.posting_date.min():
                raise ValueError("Posting dates overlap across temporal splits")
    return frames


def _inventory() -> dict:
    raw = PROJECT_ROOT / "data/raw"
    datasets = [{"path": str(path.relative_to(PROJECT_ROOT)), "bytes": path.stat().st_size,
                 "sha256": sha256_file(path)} for path in sorted(raw.rglob("*")) if path.is_file()]
    binaries = []
    for path in sorted(ARTIFACTS.rglob("*")):
        if path.is_file() and (path.suffix == ".joblib" or path.name == "model.json"):
            binaries.append({"path": str(path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(path),
                "note": "inventory digest only; loading requires a pre-existing trusted runtime contract"})
    heads = {path.parent.name: json.loads(path.read_text())
             for path in sorted((ARTIFACTS / "b2b_v2/models").glob("*/metadata.json"))}
    evidence = [{"path": str(path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(path)}
                for path in sorted(ARTIFACTS.rglob("*.json")) if path.name != "model.json"]
    processed = {path.name: json.loads(path.read_text())
                 for path in sorted((PROJECT_ROOT / "data/processed").rglob("*manifest.json"))}
    return {"datasets": datasets, "model_artifacts": binaries,
        "b2b_heads": heads, "processed_dataset_manifests": processed, "prior_evidence_fingerprints": evidence,
        "domains": {"b2b": "primary invoice payment serving", "bpi_2019": "separate purchase-to-pay process survival; incompatible target",
                    "pennine": "not found locally", "barnet": "not found locally"},
        "active_connection": "payment_risk_model -> champion_serving -> persisted_predictions / invoice SHAP; ml_intelligence catalog and offline frontend evaluation are distinct from live telemetry",
        "other_targets": "Other v2 delay heads and legacy regression artifacts are not promoted by this evaluation; missing independent runtime pins prevent binary loading."}


def feature_history_audit(frames: dict[str, pd.DataFrame]) -> dict:
    from app.ml.datasets.build_primary_features import (load_raw, construct_targets,
        add_current_invoice_features, build_customer_history, repair_feature_scales)
    raw = load_raw()
    if raw.document_no.duplicated().any() or str(raw.clearing_date.max().date()) != CUTOFF:
        raise ValueError("Source invoice identity or observation cutoff mismatch")
    rebuilt = repair_feature_scales(build_customer_history(add_current_invoice_features(construct_targets(raw))))
    rebuilt["document_no"] = rebuilt.document_no.astype(str)
    frozen = pd.concat(list(frames.values()), ignore_index=True).set_index("document_no")
    rebuilt = rebuilt.set_index("document_no").loc[frozen.index]
    differences = {}
    changed = np.zeros(len(frozen), dtype=bool)
    for feature in FEATURES:
        equal = np.isclose(frozen[feature].to_numpy(dtype=float), rebuilt[feature].to_numpy(dtype=float), equal_nan=True)
        differences[feature] = int((~equal).sum())
        changed |= ~equal
    return {"cohort_rows": len(frozen), "changed_rows": int(changed.sum()), "changed_values_by_feature": differences,
        "changed_rows_by_split": {name: int(pd.Series(changed, index=frozen.index).loc[frame.document_no].sum()) for name, frame in frames.items()},
        "source_rows": len(raw), "source_customers": int(raw.cust_num.nunique()),
        "target_disagreements": int((frozen[TARGET_COLUMN] != rebuilt[TARGET_COLUMN]).sum()),
        "finding": "Existing builder filtered the outcome cohort before building posting-time history. Fixed builder builds history before eligibility filtering. Frozen source features and predictions remain untouched.",
        "validation_use": "All reported performance uses original frozen features. Rebuilt features are diagnostic only; champion not refit or promoted."}


def evaluate_frozen() -> tuple[dict, dict]:
    from app.ml.champion_serving import verified_contract, champion_predict_many
    from app.ml.payment_risk_model import _load_contract, _load_model
    started = time.perf_counter()
    frames = _read_splits()
    source = PROJECT_ROOT / "data/raw/b2b_primary/b2b_invoice_payment_45839.csv"
    if sha256_file(source) != "2a7f1dfc4e52231d4a4cb60ee019d6cef3730708afffb1c12786b4d3061bb32d":
        raise ValueError("Verified primary source fingerprint mismatch")
    test = frames["test"]
    manifest = json.loads((SPLITS / "split_manifest.json").read_text())
    contract = verified_contract()  # before any deserialization
    dev_customers = set(pd.concat([frames["train"], frames["validation"]]).cust_num)
    split_ids = {name: frame.document_no.tolist() for name, frame in frames.items()}
    split_evidence = {name: {"rows": len(frame), "customers": frame.cust_num.nunique(),
        "start": str(frame.posting_date.min().date()), "end": str(frame.posting_date.max().date()),
        "sha256": sha256_file(SPLITS / f"{name}.csv.gz"), "prevalence": float(frame[TARGET_COLUMN].mean())}
        for name, frame in frames.items()}
    model_rows = []
    features = [PaymentRiskFeatures(**row) for row in test[FEATURES].to_dict("records")]
    predictions = champion_predict_many(features)
    probabilities = np.array([row.late_payment_probability for row in predictions])
    reconstructed = np.array([1 / (1 + np.exp(-(row.explanation_base_value +
        sum(item.shap_value for item in row.contributions)))) for row in predictions])
    cohort_metrics = {}
    for name, mask in {"seen_customers": test.cust_num.isin(dev_customers),
                       "unseen_customers": ~test.cust_num.isin(dev_customers)}.items():
        cohort_metrics[name] = {"rows": int(mask.sum()), "customers": int(test.loc[mask, "cust_num"].nunique()),
            "metrics": classification_metrics(test.loc[mask, TARGET_COLUMN], probabilities[mask]) if mask.any() else None}
    monthly = {str(month): classification_metrics(test.loc[mask, TARGET_COLUMN], probabilities[mask])
        for month in sorted(test.posting_date.dt.to_period("M").unique())
        for mask in [test.posting_date.dt.to_period("M") == month]}
    model_rows.append({"model": "LightGBM", "version": contract["version"], "artifact_sha256": contract["artifact_sha256"],
        "training": contract["metadata"], "calibration": "raw; serving policy unchanged",
        "metrics": classification_metrics(test[TARGET_COLUMN], probabilities), "monthly": monthly, "customer_cohorts": cohort_metrics})
    for family in ("random_forest", "gradient_boosting", "xgboost"):
        # Clear cached contracts so every evaluation checks current bytes.
        _load_contract.cache_clear()
        legacy = _load_contract(family)
        if legacy["train_split_sha256"] != split_evidence["train"]["sha256"]:
            raise ValueError("Legacy model training split fingerprint mismatch")
        _load_model.cache_clear()
        model = _load_model(family)
        if hasattr(model, "n_jobs"):
            model.set_params(n_jobs=1)
        X = test[FEATURES].copy()
        if family != "xgboost":
            medians = json.loads((ARTIFACTS / "models" / family / legacy["median_file"]).read_text())
            X = X.fillna(pd.Series(medians))
        p = model.predict_proba(X)[:, 1]
        model_rows.append({"model": family, "version": legacy["model_version"], "artifact_sha256": legacy["model_sha256"],
            "training": "train only; legacy temporal fit did not purge unknown outcomes; not selection-comparable to champion train+validation refit",
            "metrics": classification_metrics(test[TARGET_COLUMN], p)})
    # No training: frozen champion window scores are not rolling-origin refits.
    from app.ml.train_b2b_v2 import build_temporal_folds, add_targets
    dev = add_targets(pd.concat([frames["train"], frames["validation"]], ignore_index=True))
    folds = build_temporal_folds(dev)
    from app.ml.common import known_outcome_mask, temporal_folds
    legacy_train = frames["train"].loc[known_outcome_mask(frames["train"], frames["validation"].posting_date.min())]
    legacy_purge = {"train_rows": len(frames["train"]),
        "rows_without_observed_clearing_before_validation": int((~known_outcome_mask(frames["train"], frames["validation"].posting_date.min())).sum()),
        "note": "Conservative purge waits for clearing even if a positive horizon label was already knowable. Existing legacy results are not recomputed training results.",
        "corrected_fold_manifests": [{"fold": i, "train_rows": len(tr), "evaluation_rows": len(te),
            "train_document_ids": legacy_train.document_no.iloc[tr].tolist(),
            "evaluation_document_ids": legacy_train.document_no.iloc[te].tolist()}
            for i, (tr, te) in enumerate(temporal_folds(legacy_train))]}
    train_targets = add_targets(frames["train"])
    validation_start = frames["validation"].posting_date.min()
    train_clearing = train_targets.net_due_date + pd.to_timedelta(train_targets.days_late_strict, unit="D")
    legacy_purge["labels_unknown_by_both_clearing_and_due_horizon_at_validation"] = int(
        ((train_clearing >= validation_start) & (train_targets.available_late_payment >= validation_start)).sum())
    bpi = json.loads((ARTIFACTS / "bpi_survival/temporal_backtest.json").read_text())
    report = {"schema_version": 1, "evaluation_kind": "offline frozen-model retrospective validation",
        "target_contract": target_contract(), "inventory": _inventory(), "splits": split_evidence,
        "original_split_manifest": manifest, "models": model_rows,
        "feature_history_audit": feature_history_audit(frames),
        "legacy_outcome_purge": {key: value for key, value in legacy_purge.items() if key != "corrected_fold_manifests"},
        "drift": [compare_feature(SPLITS / "train.csv.gz", SPLITS / "test.csv.gz", name) for name in FEATURES],
        "explainability": {"rows_checked": len(predictions), "method": "native TreeSHAP",
            "max_probability_reconstruction_error": float(np.abs(probabilities - reconstructed).max()),
            "units": "log odds; sigmoid(base + contributions) reconstructs raw probability with atol 1e-7 in serving",
            "causal": False, "limitation": "Contributions describe model dependence, not causal payment drivers or recovery outcomes."},
        "rolling_origin": {"status": "historical_report_only; not rerun without training", "fold_definitions": folds,
            "champion_reported_backtest": contract["metadata"]["temporal_backtest"]},
        "group_holdout": {"status": "not independently evaluated", "manifest": json.loads((SPLITS.parent / "customer_holdout_manifest.json").read_text()),
            "limitation": "Reserved group manifest does not make current champion group-held-out: it was refit on development customers. Only natural unseen test customers can be evaluated here."},
        "regression_survival": {"status": "SKIPPED_NEW_SCORING", "reason": "No independent runtime pins for B2B regression heads. Final BPI serving model was refit on all 199,985 prepared invoices; scoring that cohort would be in-sample.",
            "bpi_historical_evidence": {"source": "backend/app/ml/artifacts/bpi_survival/temporal_backtest.json",
                "summary": bpi["models"]["lightgbm_hazard"]["summary"], "folds": bpi["models"]["lightgbm_hazard"]["folds"],
                "status": "previous recorded backtest; not rerun", "limitations": "Horizon scores exclude cases censored before the horizon. No formal IPCW integrated Brier score or population calibration is claimed."}},
        "test_status": "Official split unchanged. Test has been examined in prior development and this phase; retrospective holdout, not pristine untouched or prospective evidence.",
        "calibration_policy": "Fixed 10 equal-width ECE bins and thresholds 0.25/0.5/0.75; no threshold optimization or calibrator fit on test.",
        "proposal": "Future isolated experiment: refit candidates with causal full-source history and outcome purges, train-only preprocessing, fixed rolling/group manifests, calibration on later development-only outcomes; evaluate a new prospective cohort before promotion. Requires separate training authorization.",
        "observed_local_seconds": time.perf_counter() - started,
        "provider_usage": None}
    return report, {"schema_version": 1, "source_split_hashes": split_evidence,
                    "ids": split_ids, "legacy_corrected_folds": legacy_purge["corrected_fold_manifests"],
                    "random_seed": 42, "note": "Exact public source document IDs; no customer identities or private invoice data."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split-ids", type=Path, required=True)
    args = parser.parse_args()
    report, ids = evaluate_frozen()
    for path, value in ((args.output, report), (args.split_ids, ids)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, allow_nan=False, default=str) + "\n")
    print(json.dumps({"models": len(report["models"]), "test_rows": report["splits"]["test"]["rows"],
                      "seconds": report["observed_local_seconds"]}))


if __name__ == "__main__":
    main()
