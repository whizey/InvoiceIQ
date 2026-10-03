from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.payment_risk_model import (
    PaymentRiskArtifactError,
    predict_payment_risk,
)
from app.models import Invoice
from app.services.payment_risk_context import (
    PaymentRiskInputsUnavailable,
    build_payment_risk_features,
)


logger = logging.getLogger(__name__)


def _json_value(value):
    if isinstance(value, float):
        if not math.isfinite(value):
            return None

    return value


async def ensure_payment_risk_score(
    session: AsyncSession,
    invoice: Invoice,
) -> bool:
    model_name = (
        os.getenv(
            "PAYMENT_RISK_MODEL",
            "",
        )
        .strip()
        .lower()
    )

    if not model_name:
        return False

    if (
        invoice.late_payment_probability
        is not None
        and invoice.payment_risk_model
        == model_name
    ):
        return True

    try:
        features = (
            await build_payment_risk_features(
                session,
                invoice,
            )
        )

        prediction = predict_payment_risk(
            features,
            model_name,
        )
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

    invoice.late_payment_probability = (
        Decimal(
            f"{prediction.late_payment_probability:.6f}"
        )
    )

    invoice.payment_risk_model = (
        prediction.model_name
    )

    invoice.payment_risk_scored_at = (
        datetime.now(timezone.utc)
    )

    invoice.payment_risk_features = {
        key: _json_value(value)
        for key, value
        in features.to_row().items()
    }

    invoice.payment_risk_drivers = [
        {
            "feature": item.feature,
            "label": item.label,
            "value": _json_value(
                item.value
            ),
            "shap_value": (
                item.shap_value
            ),
            "direction": (
                item.direction
            ),
        }
        for item
        in prediction.contributions[:5]
    ]

    await session.flush()

    return True
