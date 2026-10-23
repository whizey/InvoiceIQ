"""Does the model beat a simple per-customer average on the public B2B data? Writes the answer with calibration.

The baseline is the product's: a customer's late share over its last five settled invoices, known at posting
time, pulled toward the training late rate by two pseudo-invoices so a short history is not over-trusted. Both
are scored on the chronological test split only: the served model was refit on data up to 2015-12-08, which
includes the validation rows, so validation would flatter it. Usage: python -m app.ml.baseline_comparison
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

from app.ml.payment_features import FEATURES, TARGET_COLUMN, PaymentRiskFeatures
from app.services.follow_up import PRIOR_WEIGHT

ROOT = Path(__file__).resolve().parents[3]
SPLITS = ROOT / "data/processed/primary/splits"
OUTPUT = ROOT / "docs/evaluations/baseline-vs-model.json"
FRONTEND_COPY = ROOT / "frontend/lib/evidence/baseline-vs-model.json"  # shown on the model page
WINDOW = 5


def baseline(frame: pd.DataFrame, prior: float) -> np.ndarray:
    seen = np.minimum(frame.prior_resolved_invoice_count.to_numpy(dtype=float), WINDOW)
    rate = np.where(frame.has_resolved_history.to_numpy() > 0, frame.recent_5_late_rate.to_numpy(dtype=float), 0.0)
    return (rate * seen + prior * PRIOR_WEIGHT) / (seen + PRIOR_WEIGHT)


def calibration(probabilities: np.ndarray, labels: np.ndarray) -> list[dict]:
    bands = []
    for low in (0.0, 0.2, 0.4, 0.6, 0.8):
        mask = (probabilities >= low) & ((probabilities < low + 0.2) | (low == 0.8))
        if mask.any():
            bands.append({"band": f"{int(low * 100)}–{int(low * 100) + 20}%", "invoices": int(mask.sum()),
                          "predicted": round(float(probabilities[mask].mean()), 3),
                          "actual": round(float(labels[mask].mean()), 3)})
    return bands


def score(name: str, probabilities: np.ndarray, labels: np.ndarray) -> dict:
    return {"scorer": name, "roc_auc": round(float(roc_auc_score(labels, probabilities)), 3),
            "pr_auc": round(float(average_precision_score(labels, probabilities)), 3),
            "brier": round(float(np.mean((probabilities - labels) ** 2)), 3),
            "calibration": calibration(probabilities, labels)}


def main() -> dict:
    from app.ml.champion_serving import champion_predict_many, verified_contract

    frames = {name: pd.read_csv(SPLITS / f"{name}.csv.gz", dtype={"document_no": str, "cust_num": str})
              for name in ("train", "test")}
    prior = float(frames["train"][TARGET_COLUMN].mean())
    contract = verified_contract()
    result = {"model_version": contract["version"], "target": TARGET_COLUMN, "baseline_window": WINDOW,
              "baseline_prior_weight": PRIOR_WEIGHT, "train_late_rate": round(prior, 3), "splits": {},
              "note": ("Test split only: the served model was refit on data up to 2015-12-08, which includes the "
                       "validation rows. Test invoices were posted 2015-12-08 to 2016-01-27.")}
    for name in ("test",):
        frame = frames[name]
        labels = frame[TARGET_COLUMN].to_numpy(dtype=float)
        predictions = champion_predict_many([PaymentRiskFeatures(**row) for row in frame[FEATURES].to_dict("records")])
        model = np.array([p.late_payment_probability for p in predictions])
        result["splits"][name] = {"rows": len(frame), "late_rate": round(float(labels.mean()), 3),
                                  "model": score("model", model, labels),
                                  "baseline": score("baseline", baseline(frame, prior), labels)}
    for path in (OUTPUT, FRONTEND_COPY):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


if __name__ == "__main__":
    out = main()
    for split, row in out["splits"].items():
        print(split, row["rows"], "model", row["model"]["roc_auc"], "baseline", row["baseline"]["roc_auc"])
