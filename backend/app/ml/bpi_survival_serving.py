"""Serving helpers for the BPI time-to-clear survival model."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from app.ml.train_bpi_survival import (
    INTERVALS,
    predict_recovery_curves,
)

SERVING_HORIZONS = [7, 30, 60, 90]

MODEL_PATH = Path(
    "app/ml/artifacts/bpi_survival/models/hazard_model.joblib"
)

MODEL_VERSION = "bpi_survival_lightgbm_v1"


@lru_cache(maxsize=1)
def load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"BPI model not found: {MODEL_PATH.resolve()}"
        )

    return joblib.load(MODEL_PATH)


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

    results = []

    for row_number in range(len(cases)):
        curve = cdf.iloc[row_number]

        result = {
            "model_version":
                MODEL_VERSION,

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
