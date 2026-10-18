"""Serving helpers for the BPI time-to-clear survival model."""

from __future__ import annotations

from functools import lru_cache
import json
from hashlib import sha256
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from app.ml.train_bpi_survival import (
    INTERVALS,
    predict_recovery_curves,
)

SERVING_HORIZONS = [7, 30, 60, 90]

MODEL_PATH = Path(__file__).resolve().parent / "artifacts/bpi_survival/models/hazard_model.joblib"

MODEL_VERSION = "bpi_survival_lightgbm_v1"


def verified_model_version() -> str:
    """Check enrolled bytes before deserializing a trusted local artifact."""
    from app.ml.artifact_contract import sha256_file

    root = MODEL_PATH.parent.parent
    try:
        integrity = json.loads((root / "runtime_integrity.json").read_text())
        required = {"models/hazard_model.joblib", "serving_contract.json"}
        if integrity["schema_version"] != 1 or set(integrity["files"]) != required:
            raise ValueError("Invalid BPI integrity contract")
        for name, expected in integrity["files"].items():
            if sha256_file(root / name) != expected:
                raise ValueError("BPI artifact fingerprint mismatch")
        contract = json.loads((root / "serving_contract.json").read_text())
        if contract["champion"] != "lightgbm_hazard" or contract["serving_horizons_days"] != SERVING_HORIZONS:
            raise ValueError("BPI serving contract mismatch")
        fingerprint = sha256(json.dumps(integrity["files"], sort_keys=True).encode()).hexdigest()[:16]
        return f"{MODEL_VERSION}:{fingerprint}"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("BPI artifact verification failed") from exc


@lru_cache(maxsize=1)
def _load_verified_model(version: str):
    if verified_model_version() != version:
        raise RuntimeError("BPI artifact changed while loading")
    model = joblib.load(MODEL_PATH)
    estimator = model.named_steps["model"]
    if type(estimator).__name__ != "LGBMClassifier" or list(estimator.classes_) != [0, 1]:
        raise RuntimeError("BPI estimator contract mismatch")
    estimator.set_params(n_jobs=1)
    return model


def load_model():
    return _load_verified_model(verified_model_version())


def _likely_clearance_window(cdf_row):
    """Return the first modeled horizon reaching >=50% clearance."""

    for _, end_day in INTERVALS:
        probability = cdf_row.get(float(end_day))

        if (
            probability is not None
            and float(probability) >= 0.50
        ):
            return int(end_day)

    return None


def score_cases(
    cases: pd.DataFrame,
    include_recovery_curve: bool = False,
) -> pd.DataFrame:
    """Score engineered BPI invoice-receipt rows."""

    if cases.empty:
        return pd.DataFrame()

    model = load_model()

    _, _, cdf = predict_recovery_curves(
        model,
        cases.reset_index(drop=True),
    )

    values = cdf.to_numpy(dtype=float)
    if (len(cdf) != len(cases) or not np.isfinite(values).all()
            or (values < 0).any() or (values > 1).any()
            or (np.diff(values, axis=1) < -1e-10).any()):
        raise RuntimeError("Invalid BPI survival probability contract")

    results = []

    for row_number in range(len(cases)):
        curve = cdf.iloc[row_number]

        result = {
            "model_version":
                verified_model_version(),

            "likely_clearance_window_days":
                _likely_clearance_window(curve),
        }

        for horizon in SERVING_HORIZONS:
            probability = float(
                curve[float(horizon)]
            )

            result[
                f"clearance_probability_{horizon}d"
            ] = float(
                np.clip(
                    probability,
                    0.0,
                    1.0,
                )
            )

        if include_recovery_curve:
            result["recovery_curve"] = {
                str(int(day)):
                    float(
                        np.clip(
                            probability,
                            0.0,
                            1.0,
                        )
                    )

                for day, probability
                in curve.items()
            }

        results.append(result)

    return pd.DataFrame(results)
