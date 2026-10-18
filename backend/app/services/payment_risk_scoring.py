from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.payment_risk_model import (
    PaymentRiskArtifactError,
    get_payment_risk_model_version,
    predict_payment_risk,
    selected_payment_risk_model,
)
from app.core.json_values import json_value
from starlette.concurrency import run_in_threadpool
from app.models import Invoice
from app.services.inference_telemetry import tracked_inference
from app.services.payment_risk_context import (
    PaymentRiskInputsUnavailable,
    build_payment_risk_features,
)


logger = logging.getLogger(__name__)


def apply_prediction(invoice, features, prediction, *, scored_at=None, driver_limit=None):
    """Shared persistence contract for online and public-corpus batch inference."""
    invoice.late_payment_probability = Decimal(f"{prediction.late_payment_probability:.6f}")
    invoice.payment_risk_model = prediction.model_name
    invoice.payment_risk_model_version = prediction.model_version
    invoice.payment_risk_artifact_sha256 = getattr(prediction, "artifact_sha256", None)
    invoice.payment_risk_scored_at = scored_at or datetime.now(timezone.utc)
    invoice.payment_risk_features = json_value(features.to_row())
    contributions = json_value([
        {"feature": c.feature, "value": c.value, "shap_value": c.shap_value,
         "label": c.label, "direction": c.direction} for c in prediction.contributions
    ])
    invoice.payment_risk_drivers = contributions if driver_limit is None else contributions[:driver_limit]
    base_value = getattr(prediction, "explanation_base_value", None)
    invoice.payment_risk_explanation_metadata = (
        {"method": "LightGBM native TreeSHAP", "units": "log_odds",
         "base_value": base_value, "target": "late_payment_weekend_adjusted",
         "calibration": "raw", "feature_timing": "posting_time",
         "all_contributions": contributions}
        if base_value is not None else None
    )


async def ensure_payment_risk_score(
    session: AsyncSession,
    invoice: Invoice,
) -> bool:
    model_name = selected_payment_risk_model()

    if not model_name:
        return False

    try:
        current_model_version = (
            get_payment_risk_model_version(
                model_name
            )
        )
    except PaymentRiskArtifactError as exc:
        logger.warning(
            "Payment-risk artifact is not "
            "runtime-ready: %s",
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "Unexpected error while resolving "
            "payment-risk model version."
        )
        return False

    current_artifact_hash = None
    if model_name == "b2b_v2":
        from app.ml.champion_serving import verified_contract
        current_artifact_hash = verified_contract()["artifact_sha256"]

    if (
        invoice.late_payment_probability
        is not None
        and invoice.payment_risk_model
        == model_name
        and invoice.payment_risk_model_version
        == current_model_version
        and (model_name != "b2b_v2" or invoice.payment_risk_artifact_sha256 == current_artifact_hash)
    ):
        return True

    try:
        async with session.begin_nested():
            features = (
                await build_payment_risk_features(
                    session,
                    invoice,
                )
            )

            async def infer():
                return await run_in_threadpool(predict_payment_risk, features, model_name)

            prediction = await tracked_inference(infer, model_kind="b2b", model_name=model_name,
                source="invoice_scoring", invoice_id=invoice.id, company_id=invoice.company_id, currency=invoice.currency,
                input_metadata={"input_features": features.to_row(), "input_feature_order": list(features.to_row())})

            if (
                prediction.model_version
                != current_model_version
            ):
                raise PaymentRiskArtifactError(
                    "Runtime model version changed "
                    "during scoring."
                )

            if model_name == "b2b_v2" and prediction.artifact_sha256 != current_artifact_hash:
                raise PaymentRiskArtifactError("Prediction artifact fingerprint mismatch")
            apply_prediction(invoice, features, prediction, driver_limit=5)
            await session.flush()

    except PaymentRiskInputsUnavailable as exc:
        logger.info(
            "Payment-risk score unavailable "
            "for invoice %s: %s",
            invoice.invoice_number,
            exc,
        )
        return False

    except PaymentRiskArtifactError as exc:
        logger.warning(
            "Payment-risk artifact is not "
            "runtime-ready: %s",
            exc,
        )
        return False

    except Exception:
        logger.exception(
            "Unexpected payment-risk scoring "
            "failure for invoice %s. "
            "Invoice processing will continue.",
            invoice.invoice_number,
        )
        return False

    return True
