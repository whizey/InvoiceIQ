"""Inference for the (synthetic-data-trained) recovery-risk model.

See app/ml/train.py / app/ml/synthetic_data.py for training details and
the "not a production financial model" caveat.
"""

import functools
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import xgboost as xgb
import shap

from app.ml.features import FEATURE_NAMES, RiskFeatures
from app.models.enums import RiskLevel

MODEL_PATH = Path(__file__).parent / "artifacts" / "recovery_risk_model.json"

# risk_score = (1 - recovery_probability) * 100; thresholds chosen so the
# seed-data narrative's hand-authored scores (Northwind 22.5=LOW,
# Bluepeak/Sundial ~48-55=MEDIUM, Vertex 82=HIGH) land in the same bands
# the model would assign, keeping demo and model-scored cases consistent.
LOW_RISK_MAX = 35.0
MEDIUM_RISK_MAX = 65.0

FEATURE_LABELS = {
    "invoice_amount": "Invoice Amount",
    "days_overdue": "Days Overdue",
    "outstanding_balance": "Outstanding Balance",
    "avg_historical_payment_delay": "Average Historical Payment Delay",
    "num_prior_late_payments": "Previous Late Payments",
    "num_prior_on_time_payments": "Previous On-Time Payments",
    "customer_tenure_days": "Customer Tenure",
    "prior_recovery_success_rate": "Previous Recovery Success Rate",
    "num_open_invoices": "Open Invoices",
}

@dataclass
class RiskScore:
    risk_score: float  # 0-100, higher = riskier
    risk_level: RiskLevel
    recovery_probability: float  # 0-1

@dataclass
class FeatureContribution:
    feature: str
    label: str
    value: float
    shap_value: float
    direction: str


@dataclass
class RiskExplanation:
    risk_score: float
    risk_level: RiskLevel
    recovery_probability: float
    base_value: float
    contributions: list[FeatureContribution]

@functools.lru_cache(maxsize=1)
def _load_model() -> xgb.XGBClassifier:
    model = xgb.XGBClassifier()
    model.load_model(str(MODEL_PATH))
    return model

@functools.lru_cache(maxsize=1)
def _load_explainer():
    model = _load_model()
    return shap.TreeExplainer(model)

def score(features: RiskFeatures) -> RiskScore:
    model = _load_model()
    row = pd.DataFrame([features.to_row()], columns=FEATURE_NAMES)
    recovery_probability = float(model.predict_proba(row)[0][1])
    risk_score = round((1 - recovery_probability) * 100, 2)

    if risk_score < LOW_RISK_MAX:
        risk_level = RiskLevel.LOW
    elif risk_score < MEDIUM_RISK_MAX:
        risk_level = RiskLevel.MEDIUM
    else:
        risk_level = RiskLevel.HIGH

    return RiskScore(
        risk_score=risk_score,
        risk_level=risk_level,
        recovery_probability=round(recovery_probability, 4),
    )
def explain(features: RiskFeatures) -> RiskExplanation:
    model = _load_model()
    explainer = _load_explainer()

    row = pd.DataFrame(
        [features.to_row()],
        columns=FEATURE_NAMES,
    )

    recovery_probability = float(model.predict_proba(row)[0][1])
    risk_score = round((1 - recovery_probability) * 100, 2)

    if risk_score < LOW_RISK_MAX:
        risk_level = RiskLevel.LOW
    elif risk_score < MEDIUM_RISK_MAX:
        risk_level = RiskLevel.MEDIUM
    else:
        risk_level = RiskLevel.HIGH

    shap_values = explainer.shap_values(row)

    if hasattr(shap_values, "ndim") and shap_values.ndim == 2:
        values = shap_values[0]
    else:
        values = shap_values

    contributions = []

    for feature_name, shap_value in zip(FEATURE_NAMES, values):
        feature_value = float(row.iloc[0][feature_name])

        contributions.append(
            FeatureContribution(
                feature=feature_name,
                label=FEATURE_LABELS[feature_name],
                value=feature_value,
                shap_value=round(float(shap_value), 6),
                direction=(
                    "increases_recovery_probability"
                    if shap_value > 0
                    else "decreases_recovery_probability"
                ),
            )
        )

    contributions.sort(
        key=lambda item: abs(item.shap_value),
        reverse=True,
    )

    expected_value = explainer.expected_value

    if hasattr(expected_value, "__len__"):
        base_value = float(expected_value[-1])
    else:
        base_value = float(expected_value)

    return RiskExplanation(
        risk_score=risk_score,
        risk_level=risk_level,
        recovery_probability=round(recovery_probability, 4),
        base_value=round(base_value, 6),
        contributions=contributions,
    )
