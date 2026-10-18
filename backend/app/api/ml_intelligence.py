"""InvoiceIQ ML catalog and experimental BPI process-survival API."""

import csv
import math
from datetime import date
from app.core.analytics_auth import require_analytics
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Request, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.db import get_db
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.ml.payment_risk_model import (
    get_payment_risk_model_version,
    selected_payment_risk_model,
    predict_payment_risk,
)
from app.ml.payment_features import FEATURES, PaymentRiskFeatures
from app.core.json_values import json_value

router = APIRouter(prefix="/ml", tags=["ml-intelligence"])


@router.get('/stored-predictions')
async def stored_predictions(start_date: date, end_date: date,
                             currency: str = Query('INR', pattern=r'^[A-Z]{3}$'), db: AsyncSession = Depends(get_db),
                             identity=Depends(require_analytics)):
    from app.schemas.analytics import AnalyticsRange
    from app.services.persisted_predictions import stored_prediction_report
    if start_date > end_date:
        raise HTTPException(422, 'start_date must not exceed end_date')
    try:
        interval = AnalyticsRange(start_date=start_date, end_date=end_date, currency=currency)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid stored-prediction date range") from exc
    return await stored_prediction_report(db, company_ids=identity.company_ids, currency=interval.currency,
        start_date=interval.start_date, end_date=interval.end_date)

ARTIFACTS = Path(__file__).resolve().parents[1] / "ml" / "artifacts"
BPI_MODEL = ARTIFACTS / "bpi_survival/models/hazard_model.joblib"
LANDMARKS = (
    ARTIFACTS
    / "stage3_validation/bpi/fold_0/landmark_metrics.csv"
)


@router.get("/model-risk")
def model_risk_report():
    """Aggregate offline evidence, never an inference or customer-data query."""
    import json
    from app.ml.champion_serving import verified_contract
    path = Path(__file__).resolve().parents[3] / "docs/evaluations/model-risk-v1.json"
    if not path.exists():
        raise HTTPException(503, "Offline model risk report unavailable")
    report = json.loads(path.read_text())
    if report["models"][0]["version"] != verified_contract()["version"]:
        raise HTTPException(503, "Offline evidence does not match the serving champion")
    return report


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
    selected = selected_payment_risk_model()
    version = None
    contract = None
    bpi_version = None

    if selected:
        try:
            version = get_payment_risk_model_version(selected)
            if selected == "b2b_v2":
                from app.ml.champion_serving import verified_contract, _load_verified_model
                contract = verified_contract()
                _load_verified_model(version)
            else:
                from app.ml.payment_risk_model import _load_model
                _load_model(selected)
        except Exception:
            version = None
            contract = None

    try:
        from app.ml.bpi_survival_serving import load_model, verified_model_version
        load_model()
        bpi_version = verified_model_version()
    except Exception:
        pass

    return {
        "primary_model": selected or None,
        "models": [
            {
                "id": "b2b-payment-risk",
                "role": "primary",
                "dataset": "Real B2B invoice history",
                "target": "late_payment_weekend_adjusted",
                "algorithm": "LightGBM" if contract else selected or "Not configured",
                "calibration": contract["policy"] if contract else None,
                "evaluation": contract["metadata"]["temporal_backtest"] if contract else None,
                "provenance": contract["provenance"] if contract else None,
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
                "version": bpi_version,
                "available": bpi_version is not None,
                "experimental": True,
                "linked_to_recovery": False,
                "limitation": (
                    "Research model. Conditional clearance "
                    "probabilities remain poorly calibrated."
                ),
            },
        ],
    }


class PaymentRiskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: dict[str, float | None] = Field(min_length=1, max_length=18)

    @field_validator("features", mode="before")
    @classmethod
    def validate_numeric_values(cls, values):
        if isinstance(values, dict):
            for value in values.values():
                if value is not None and (
                    isinstance(value, (bool, str)) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or abs(value) > 1e12
                ):
                    raise ValueError("Features must be finite numeric values or null")
        return values


@router.get("/payment-risk/features")
def payment_risk_features():
    return {"features": FEATURES, "prediction_point": "invoice posting time",
            "missing_values": "Explicit null uses trained imputation; all feature keys are required"}


@router.post("/payment-risk/predict")
async def payment_risk_predict(payload: PaymentRiskRequest, request: Request):
    if set(payload.features) != set(FEATURES):
        raise HTTPException(status_code=422, detail="Payment-risk feature contract mismatch")
    selected = selected_payment_risk_model()
    if not selected:
        raise HTTPException(status_code=503, detail="Payment-risk model disabled")
    try:
        from app.services.inference_telemetry import tracked_inference
        async def infer():
            return await run_in_threadpool(predict_payment_risk, PaymentRiskFeatures(**payload.features), selected)
        prediction = await tracked_inference(infer, model_kind="b2b", model_name=selected,
            source="feature_inspector", workspace_id=getattr(request.state, "analytics_workspace", None),
            input_metadata={"input_features": payload.features, "input_feature_order": list(payload.features)})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid payment-risk features") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Payment-risk inference unavailable") from exc
    return json_value({**asdict(prediction),
                       "calibration_status": "SUPPORTED_UNCALIBRATED" if selected == "b2b_v2" else "legacy_raw",
                       "explanation_units": "log_odds" if selected in {"b2b_v2", "xgboost", "gradient_boosting"} else "probability",
                       "limitation": "Predictive associations, not causal effects or recovery probability"})


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

    try:
        numeric, categorical = get_bpi_feature_names()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="BPI model unavailable") from exc
    return {
        "numeric": numeric,
        "categorical": categorical,
        "expected_feature_count": len(numeric) + len(categorical),
        "description": (
            "Engineered BPI receipt-time features only. "
            "Do not pass B2B invoice features."
        ),
    }


@router.get("/offline-evaluations")
async def imported_offline_evaluations(session: AsyncSession = Depends(get_db)):
    from app.models import OfflineEvaluation
    records = (await session.scalars(select(OfflineEvaluation))).all()
    return {"mode": "offline_evaluation_only", "serving_claim": False,
            "status": "available" if records else "not_available",
            "reason": None if records else "No verified evaluation artifacts have been imported",
            "evaluations": [{"source": r.source, "source_sha256": r.source_sha256,
                "model_family": r.model_family, "evaluation_context": r.evaluation_context,
                "metrics": r.metrics, "imported_at": r.imported_at} for r in records]}


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
        verified_model_version,
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
        "model_version": verified_model_version(),
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
async def predict_bpi(payload: BpiRequest, request: Request):
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
        from app.services.inference_telemetry import tracked_inference
        async def infer():
            return await run_in_threadpool(score_bpi, payload)
        result = await tracked_inference(infer, model_kind="bpi", model_name="lightgbm_discrete_hazard",
            source="feature_inspector", workspace_id=getattr(request.state, "analytics_workspace", None),
            input_metadata={"input_features": payload.features, "input_feature_order": list(payload.features)})
        return json_value(result)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="BPI inference unavailable") from exc


@router.get("/dashboard")
async def model_dashboard(db: AsyncSession = Depends(get_db), identity=Depends(require_analytics)):
    """Workspace-scoped observations; research is read from existing evaluation files."""
    from app.services.model_dashboard import live_evidence, research_evidence
    catalog = await run_in_threadpool(list_models)
    live = await live_evidence(db, identity)
    research = await run_in_threadpool(research_evidence)
    return json_value({"catalog": catalog, "live": live, "research": research})
