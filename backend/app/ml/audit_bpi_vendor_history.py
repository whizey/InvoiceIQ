"""Reproduce the BPI vendor-history tie audit without modifying source data.

Run from backend with the project virtualenv and constrained BLAS threads:
    python -m app.ml.audit_bpi_vendor_history

The command reads the canonical historical feature CSV, reruns the preserved
legacy function from its checked-in AST on fixed row shuffles, evaluates the
isolated v2 builder, and writes a versioned JSON report only.
"""

from __future__ import annotations

import ast
import hashlib
import heapq
import json
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from app.ml.datasets.bpi_vendor_history_v2 import OUTPUT_COLUMNS, add_vendor_history_v2


ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data/processed/bpi_2019/bpi_survival_features.csv.gz"
RAW_XES = ROOT / "data/raw/bpi_2019/BPI_Challenge_2019.xes"
SOURCE = ROOT / "backend/app/ml/datasets/build_bpi_survival.py"
REPORT = ROOT / "docs/evaluations/bpi-vendor-history-temporal-audit-v2.json"
MODEL_FEATURES = (
    "vendor_prior_invoice_count",
    "vendor_prior_resolved_count",
    "vendor_prior_open_count",
    "vendor_historical_clear_rate",
    "vendor_historical_avg_clear_days",
    "vendor_recent5_avg_clear_days",
    "vendor_days_since_previous_invoice",
    "vendor_historical_avg_invoice_amount",
    "amount_vs_vendor_historical_avg",
)
SEEDS = (17, 41, 2026, 99173, 20261011)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_builder():
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "add_vendor_history"
    )
    namespace = {"np": np, "pd": pd, "deque": deque, "heapq": heapq, "banner": lambda _: None}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["add_vendor_history"]


def compare(left: pd.DataFrame, right: pd.DataFrame, columns: tuple[str, ...]) -> dict:
    output = {}
    for column in columns:
        a = pd.to_numeric(left[column], errors="coerce")
        b = pd.to_numeric(right[column], errors="coerce")
        a, b = a.align(b, join="inner")
        missing_a, missing_b = a.isna(), b.isna()
        both = ~(missing_a | missing_b)
        delta = (a[both] - b[both]).abs()
        changed = (missing_a != missing_b) | (both & a.ne(b))
        material = (missing_a != missing_b) | (both & ((a - b).abs() > 1e-9))
        output[column] = {
            "exactly_changed_rows": int(changed.sum()),
            "materially_changed_rows_at_1e-9_tolerance": int(material.sum()),
            "null_status_changed_rows": int((missing_a != missing_b).sum()),
            "mean_absolute_difference": float(delta.mean()) if len(delta) else 0.0,
            "median_absolute_difference": float(delta.median()) if len(delta) else 0.0,
            "maximum_absolute_difference": float(delta.max()) if len(delta) else 0.0,
            "finite_comparison_rows": int(len(delta)),
        }
    return output


def material_row_count(left: pd.DataFrame, right: pd.DataFrame, columns: tuple[str, ...]) -> int:
    changed = np.zeros(len(left), dtype=bool)
    for column in columns:
        a = pd.to_numeric(left[column], errors="coerce").to_numpy(dtype=float)
        b = pd.to_numeric(right[column], errors="coerce").to_numpy(dtype=float)
        changed |= (np.isnan(a) != np.isnan(b)) | (np.isfinite(a) & np.isfinite(b) & (np.abs(a - b) > 1e-9))
    return int(changed.sum())


def shuffle_within_tied_vendor_receipt_cohorts(cases: pd.DataFrame, seed: int) -> pd.DataFrame:
    row_order = np.arange(len(cases))
    rng = np.random.default_rng(seed)
    cohorts = cases.groupby(["vendor", "invoice_timestamp"], dropna=False, sort=False).indices
    for positions in cohorts.values():
        if len(positions) > 1:
            row_order[positions] = rng.permutation(positions)
    return cases.iloc[row_order].reset_index(drop=True)


def main() -> None:
    before = sha256(DATA)
    cases = pd.read_csv(
        DATA,
        parse_dates=["invoice_timestamp", "clear_timestamp"],
        low_memory=False,
    )
    if cases["case_id"].isna().any() or cases["case_id"].duplicated().any():
        raise ValueError("Canonical case_id is missing or non-unique")
    cases["event_observed"] = pd.to_numeric(cases["event_observed"], errors="coerce")
    tie_keys = ["vendor", "invoice_timestamp"]
    group_sizes = cases.groupby(tie_keys, dropna=False, sort=False).size()
    tied = group_sizes[group_sizes > 1]
    tied_mask = cases.set_index(tie_keys).index.isin(tied.index)
    tied_cases = cases.loc[tied_mask]

    legacy = legacy_builder()
    baseline = legacy(cases.copy())
    persisted_reproduction = compare(cases, baseline, MODEL_FEATURES)

    shuffle_results = {}
    for seed in SEEDS:
        shuffled = shuffle_within_tied_vendor_receipt_cohorts(cases, seed)
        recomputed = legacy(shuffled)
        indexed = recomputed.set_index("case_id")
        original = cases.set_index("case_id")
        shuffle_results[str(seed)] = compare(original, indexed, MODEL_FEATURES)

    corrected = add_vendor_history_v2(cases)
    corrected_impact = compare(cases, corrected, OUTPUT_COLUMNS)
    windows = [
        ("fold_0", "2018-07-01", "2018-07-01", "2018-09-01"),
        ("fold_1", "2018-09-01", "2018-09-01", "2018-11-01"),
        ("fold_2", "2018-11-01", "2018-11-01", "2018-12-01"),
        ("fold_3", "2018-12-01", "2018-12-01", "2019-01-18 13:34:00+00:00"),
    ]
    split_impact = {}
    times = pd.to_datetime(cases["invoice_timestamp"], utc=True)
    corrected_indexed = corrected.set_index("case_id")
    persisted_indexed = cases.set_index("case_id")
    for name, train_cutoff, eval_start, eval_end in windows:
        start = pd.Timestamp(eval_start, tz="UTC")
        end = pd.Timestamp(eval_end).tz_localize("UTC") if pd.Timestamp(eval_end).tzinfo is None else pd.Timestamp(eval_end).tz_convert("UTC")
        train_mask = times < pd.Timestamp(train_cutoff, tz="UTC")
        eval_mask = (times >= start) & (times < end)
        train_ids = cases.loc[train_mask, "case_id"].tolist()
        eval_ids = cases.loc[eval_mask, "case_id"].tolist()
        split_impact[name] = {
            "train_cutoff": train_cutoff,
            "evaluation_window": [eval_start, eval_end],
            "training_rows_with_any_material_model_feature_change": material_row_count(
                persisted_indexed.loc[train_ids], corrected_indexed.loc[train_ids], MODEL_FEATURES
            ),
            "evaluation_rows_with_any_material_model_feature_change": material_row_count(
                persisted_indexed.loc[eval_ids], corrected_indexed.loc[eval_ids], MODEL_FEATURES
            ),
            "training_invoices": len(train_ids),
            "evaluation_invoices": len(eval_ids),
        }
    # Repeated shuffled-input invariance is covered by the real-data regression
    # test; the full 200k-row frame is calculated once here to keep the audit
    # command practical and avoid writing any experimental matrices.
    timestamp_groups = cases.groupby("invoice_timestamp", dropna=False).size()
    timestamp_ties = timestamp_groups[timestamp_groups > 1]
    report = {
        "schema_version": 2,
        "status": "completed; exploratory feature audit only; no model fit or serving changes",
        "dataset": {
            "path": str(DATA.relative_to(ROOT)),
            "sha256": before,
            "rows_invoices_cases": int(len(cases)),
            "unique_cases": int(cases["case_id"].nunique()),
            "invoice_time_min": str(cases["invoice_timestamp"].min()),
            "invoice_time_max": str(cases["invoice_timestamp"].max()),
            "clear_time_observed_rows": int(cases["event_observed"].eq(1).sum()),
            "censored_rows": int(cases["event_observed"].eq(0).sum()),
            "currency": "Canonical feature schema contains invoice_amount_eur; only EUR amount aggregates are used.",
            "raw_xes": {
                "path": str(RAW_XES.relative_to(ROOT)),
                "bytes": RAW_XES.stat().st_size,
                "sha256": sha256(RAW_XES),
                "parsed_during_audit": False,
            },
        },
        "ties": {
            "receipt_timestamp_groups_with_multiple_cases_any_vendor": int(len(timestamp_ties)),
            "rows_in_timestamp_ties_any_vendor": int(timestamp_ties.sum()),
            "vendor_receipt_timestamp_groups_with_multiple_cases": int(len(tied)),
            "rows_in_vendor_receipt_timestamp_ties": int(len(tied_cases)),
            "affected_vendors": int(tied_cases["vendor"].nunique()),
            "affected_cases": int(tied_cases["case_id"].nunique()),
            "missing_vendor_or_receipt_timestamp_rows": int(cases["vendor"].isna().sum() + cases["invoice_timestamp"].isna().sum()),
        },
        "legacy_reproduction": {
            "source": str(SOURCE.relative_to(ROOT)),
            "persisted_vendor_features_match_current_source_recalculation": not any(
                item["materially_changed_rows_at_1e-9_tolerance"] for item in persisted_reproduction.values()
            ),
            "persisted_vs_current_source_by_feature": persisted_reproduction,
            "interpretation": "The persisted CSV is sorted after feature generation. Rebuilding from that sorted row order does not recreate its original same-time ordering, which is itself evidence that the row-at-a-time builder cannot reproduce a unique cohort snapshot.",
            "fixed_shuffle_seeds_within_vendor_receipt_ties": list(SEEDS),
            "by_feature": shuffle_results,
        },
        "corrected_v2": {
            "source": "backend/app/ml/datasets/bpi_vendor_history_v2.py",
            "feature_contract": "equal vendor/receipt timestamp cohorts share a pre-cohort snapshot; prior clear is eligible only when clear_timestamp < invoice_timestamp",
            "existing_vs_v2_by_feature": corrected_impact,
            "temporal_fold_impact": split_impact,
            "fixed_shuffle_seeds": "four fixed permutations plus original order are exercised on the 5,000-row source-backed sample in test_bpi_vendor_history_v2.py",
            "model_feature_overlap": list(MODEL_FEATURES),
            "ranked_bpi_models_exposed_by_training_feature_contract": [
                "LightGBM Discrete-Time Hazard",
                "XGBoost AFT",
                "CoxNet Elastic-Net",
                "Cox Proportional Hazards",
                "Gradient Boosting Survival",
            ],
            "isolated_from_model_serving": True,
        },
        "provenance": {
            "raw_xes_reparsed": False,
            "canonical_feature_data_used": True,
            "source_data_sha256_unchanged_after_run": sha256(DATA) == before,
            "model_artifacts_loaded_or_modified": False,
            "model_training_or_scoring": False,
            "evaluation_splits_or_historical_features_modified": False,
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({
        "invoices": len(cases),
        "vendor_timestamp_tied_groups": len(tied),
        "rows_in_vendor_timestamp_ties": int(tied.sum()),
        "affected_vendors": int(tied_cases.vendor.nunique()),
        "legacy_seed_results": len(shuffle_results),
        "corrected_shuffle_test": "source-backed 5,000-row test, five seeds total with one baseline order",
        "report": str(REPORT.relative_to(ROOT)),
    }))


if __name__ == "__main__":
    main()
