"""InvoiceIQ ML catalog and experimental BPI process-survival API."""

import csv
import math
import os
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.ml.payment_risk_model import (
    PaymentRiskArtifactError,
    get_payment_risk_model_version,
)

router = APIRouter(prefix="/ml", tags=["ml-intelligence"])

ARTIFACTS = Path(__file__).resolve().parents[1] / "ml" / "artifacts"
BPI_MODEL = ARTIFACTS / "bpi_survival/models/hazard_model.joblib"
LANDMARKS = (
    ARTIFACTS
    / "stage3_validation/bpi/fold_0/landmark_metrics.csv"
)


class BpiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    features: dict[str, float | str | None] = Field(
        min_length=1,
        max_length=60,
    )
    elapsed_days_since_receipt: float = Field(
        default=0, ge=0, le=275
    )
    confirmed_uncleared: bool = False


@router.get("/models")
def list_models():
    selected = os.getenv("PAYMENT_RISK_MODEL", "").strip().lower()
    version = None

    if selected:
        try:
            version = get_payment_risk_model_version(selected)
        except (PaymentRiskArtifactError, ValueError):
            pass

    return {
        "primary_model": selected or None,
        "models": [
            {
                "id": "b2b-payment-risk",
                "role": "primary",
                "dataset": "Real B2B invoice history",
                "target": "late_payment_weekend_adjusted",
                "algorithm": selected or "Not configured",
                "version": version,
                "available": version is not None,
                "experimental": True,
                "linked_to_recovery": True,
                "limitation": (
                    "Temporal performance varies substantially. "
                    "Late-payment probability is not recovery probability."
                ),
            },
            {
                "id": "bpi-process-survival",
                "role": "secondary",
                "dataset": "BPI Challenge 2019",
                "target": "purchase_to_pay_process_clearance",
                "algorithm": "LightGBM discrete-time hazard",
                "version": (
                    "bpi_survival_lightgbm_v1"
                    if BPI_MODEL.is_file() else None
                ),
                "available": BPI_MODEL.is_file(),
                "experimental": True,
                "linked_to_recovery": False,
                "limitation": (
                    "Research model. Conditional clearance "
                    "probabilities remain poorly calibrated."
                ),
            },
        ],
    }


def get_bpi_feature_names():
    from app.ml.bpi_survival_serving import load_model

    model = load_model()
    preprocessor = model.named_steps["preprocessor"]
    columns = {
        name: list(features)
        for name, _, features in preprocessor.transformers_
    }
    numeric = [
        name for name in columns["numeric"]
        if not name.startswith("interval_")
    ]
    categorical = [
        name for name in columns["categorical"]
        if name != "hazard_interval"
    ]
    return numeric, categorical


@router.get("/process-survival/features")
def bpi_features():
    if not BPI_MODEL.is_file():
        raise HTTPException(
            status_code=503,
            detail="BPI model artifact unavailable",
        )

    numeric, categorical = get_bpi_feature_names()
    return {
        "numeric": numeric,
        "categorical": categorical,
        "expected_feature_count": len(numeric) + len(categorical),
        "description": (
            "Engineered BPI receipt-time features only. "
            "Do not pass B2B invoice features."
        ),
    }


@router.get("/process-survival/landmarks")
def bpi_landmarks():
    if not LANDMARKS.is_file():
        return {
            "status": "not_available",
            "fold": "fold_0",
            "rows": [],
        }

    fields = (
        "age", "horizon", "at_risk", "known",
        "coverage", "auc", "brier",
        "predicted_mean", "observed_rate",
    )

    with LANDMARKS.open(newline="") as handle:
        rows = [
            {
                key: float(row[key]) if row.get(key) else None
                for key in fields
            }
            for row in csv.DictReader(handle)
        ]

    return {
        "status": "experimental",
        "fold": "fold_0",
        "rows": rows,
    }


def score_bpi(payload: BpiRequest):
    from app.ml.bpi_survival_serving import (
        MODEL_VERSION,
        score_cases,
    )
    from app.ml.bpi_conditional_survival import (
        conditional_clearance_probabilities,
    )

    numeric, categorical = get_bpi_feature_names()
    expected = set(numeric) | set(categorical)
    provided = set(payload.features)

    if expected != provided:
        raise ValueError(
            f"Feature mismatch: "
            f"missing={sorted(expected - provided)}, "
            f"unknown={sorted(provided - expected)}"
        )

    clean = {}

    for name in numeric:
        value = payload.features[name]

        if isinstance(value, (str, bool)):
            raise ValueError(f"{name} must be numeric")

        if value is not None:
            value = float(value)
            if not math.isfinite(value) or abs(value) > 1e12:
                raise ValueError(f"Invalid numeric value: {name}")

        clean[name] = value

    for name in categorical:
        value = payload.features[name]

        if value is not None:
            if not isinstance(value, str) or len(value) > 120:
                raise ValueError(f"Invalid category: {name}")

        clean[name] = value

    age = payload.elapsed_days_since_receipt

    if age > 0 and not payload.confirmed_uncleared:
        raise ValueError(
            "Conditional forecast requires confirmed_uncleared=true"
        )

    prediction = score_cases(
        pd.DataFrame([clean]),
        include_recovery_curve=True,
    ).iloc[0]

    curve = prediction["recovery_curve"]

    if age > 0:
        probabilities = conditional_clearance_probabilities(
            curve,
            elapsed_days_since_receipt=age,
            horizons=(7, 30),
        )
        basis = "conditional_on_uncleared"
    else:
        probabilities = {
            f"clearance_probability_{days}d": float(
                prediction[f"clearance_probability_{days}d"]
            )
            for days in (7, 30, 60, 90)
        }
        basis = "from_invoice_receipt"

    return {
        "model": "lightgbm_discrete_hazard",
        "model_version": MODEL_VERSION,
        "target": "purchase_to_pay_process_clearance",
        "experimental": True,
        "prediction_basis": basis,
        "elapsed_days_since_receipt": age,
        "horizon_probabilities": probabilities,
        "cumulative_curve": curve,
        "likely_clearance_window_days": (
            int(prediction["likely_clearance_window_days"])
            if prediction["likely_clearance_window_days"] is not None
            else None
        ),
        "note": (
            "BPI process-clearance research forecast, "
            "not B2B payment or debt recovery. "
            "Conditional predictions are not calibrated."
        ),
    }


@router.post("/process-survival/predict")
async def predict_bpi(payload: BpiRequest):
    if settings.env not in {"local", "test", "development"}:
        raise HTTPException(
            status_code=403,
            detail="Research inference disabled outside local/test",
        )

    if not BPI_MODEL.is_file():
        raise HTTPException(
            status_code=503,
            detail="BPI model artifact unavailable",
        )

    try:
        return await run_in_threadpool(score_bpi, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
