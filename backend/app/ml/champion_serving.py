"""Serve the evaluated v2 champion; never reselect or recalibrate at runtime."""

import json
import math
import re
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from app.ml.artifact_contract import sha256_file
from app.ml.named_inference import inference_input
from app.ml.payment_features import FEATURES, PaymentRiskFeatures

ROOT = Path(__file__).resolve().parent / "artifacts/b2b_v2"


def verified_contract() -> dict:
    from app.ml.payment_risk_model import PaymentRiskArtifactError

    try:
        integrity = json.loads((ROOT / "runtime_integrity.json").read_text())
        required = {
            "champion_manifest.json", "models/late_payment/model.joblib",
            "models/late_payment/metadata.json", "calibration/serving_calibration_policy.json",
            "target_feature_contract.json",
        }
        if integrity["schema_version"] != 1 or set(integrity["files"]) != required:
            raise ValueError("Invalid champion integrity contract")
        for filename, expected in integrity["files"].items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256_file(ROOT / filename) != expected:
                raise ValueError("Champion artifact fingerprint mismatch")
        metadata = json.loads((ROOT / "models/late_payment/metadata.json").read_text())
        champion = json.loads((ROOT / "champion_manifest.json").read_text())
        policy = json.loads((ROOT / "calibration/serving_calibration_policy.json").read_text())["late_payment"]
        names = metadata["features"]
        if (not names or len(names) != len(set(names)) or not set(names).issubset(FEATURES)
                or metadata["model_family"] != "lightgbm" or champion["late_payment"]["model"] != "LightGBM"
                or metadata["test_used"] or champion["test_split_read"]
                or not metadata["temporal_backtest"]["valid"] or policy["serving_mode"] != "raw"
                or metadata["serving_calibration"] != "raw"):
            raise ValueError("Champion serving policy or feature contract mismatch")
        # Version includes metadata and serving policy, not just the trained binary.
        from hashlib import sha256
        version = sha256(json.dumps(integrity["files"], sort_keys=True).encode()).hexdigest()[:16]
        return {"version": f"b2b_v2:{version}", "metadata": metadata, "policy": policy,
                "artifact_sha256": integrity["files"]["models/late_payment/model.joblib"],
                "provenance": integrity["enrollment"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PaymentRiskArtifactError("B2B champion integrity verification failed") from exc


@lru_cache(maxsize=1)
def _load_verified_model(version: str):
    from app.ml.payment_risk_model import PaymentRiskArtifactError

    contract = verified_contract()
    if contract["version"] != version:
        raise PaymentRiskArtifactError("Champion changed while loading")
    model = joblib.load(ROOT / "models/late_payment/model.joblib")
    if list(model.feature_names_in_) != contract["metadata"]["features"]:
        raise PaymentRiskArtifactError("Trained champion feature order mismatch")
    estimator = model.named_steps["model"]
    if type(estimator).__name__ != "LGBMClassifier" or list(estimator.classes_) != [0, 1]:
        raise PaymentRiskArtifactError("Unexpected champion estimator or class labels")
    estimator.set_params(n_jobs=1)
    return model


def champion_predict(features: PaymentRiskFeatures):
    return champion_predict_many([features])[0]


def champion_predict_many(features: list[PaymentRiskFeatures]):
    """Use the identical runtime pipeline in batches; verify integrity at both boundaries."""
    from app.ml.payment_risk_model import PaymentRiskArtifactError, PaymentRiskContribution, PaymentRiskPrediction

    contract = verified_contract()
    names = contract["metadata"]["features"]
    rows = [item.to_row() for item in features]
    if not rows:
        return []
    for name, value in ((name, value) for row in rows for name, value in row.items()):
        # Internal posting-time features use NaN for missing history, as in training.
        if isinstance(value, (bool, str)) or (value is not None and math.isinf(float(value))):
            raise ValueError(f"Invalid numeric feature: {name}")
    model = _load_verified_model(contract["version"])
    frame = pd.DataFrame([[row[name] for name in names] for row in rows], columns=names, dtype=float)
    estimator, prepared = inference_input(model, frame)
    probabilities = estimator.predict_proba(prepared)[:, 1]
    # LightGBM's native TreeSHAP preserves exact feature order. Values are log-odds contributions.
    explanations = np.asarray(model.named_steps["model"].booster_.predict(prepared, pred_contrib=True, num_threads=1))
    if explanations.shape != (len(rows), len(names) + 1) or not np.isfinite(explanations).all() or not np.isfinite(probabilities).all() or ((probabilities < 0) | (probabilities > 1)).any():
        raise PaymentRiskArtifactError("Invalid champion prediction or explanation")
    reconstructed = 1 / (1 + np.exp(-explanations.sum(axis=1)))
    if not np.allclose(reconstructed, probabilities, atol=1e-7):
        raise PaymentRiskArtifactError("Champion explanation does not reconstruct prediction")
    if verified_contract()["version"] != contract["version"]:
        raise PaymentRiskArtifactError("Champion changed during batch scoring")
    predictions = []
    for row, probability, shap in zip(rows, probabilities, explanations, strict=True):
        contributions = [PaymentRiskContribution(
            feature=name, label=name.replace("_", " ").title(), value=row[name], shap_value=float(value),
            direction=("increases_late_payment_probability" if value > 0 else
                       "decreases_late_payment_probability" if value < 0 else "neutral"),
        ) for name, value in zip(names, shap[:-1], strict=True)]
        contributions.sort(key=lambda item: abs(item.shap_value), reverse=True)
        predictions.append(PaymentRiskPrediction(float(probability), "b2b_v2", contract["version"], contributions,
            artifact_sha256=contract["artifact_sha256"], explanation_base_value=float(shap[-1])))
    return predictions
