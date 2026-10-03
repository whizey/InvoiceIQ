from __future__ import annotations

from dataclasses import asdict, dataclass


TARGET_COLUMN = "late_payment_weekend_adjusted"

COUNT_CAP = 500
RATIO_CLIP = 20.0


NUMERIC_FEATURES = [
    "amount",
    "payment_term",
    "prior_resolved_invoice_count",
    "prior_open_invoice_count",
    "open_invoice_ratio",
    "has_resolved_history",
    "historical_late_rate",
    "historical_avg_days_late",
    "historical_avg_invoice_amount",
    "amount_vs_historical_avg_ratio",
    "customer_tenure_days",
    "days_since_previous_invoice",
    "recent_5_late_rate",
    "recent_5_avg_days_late",
    "recent_late_streak",
    "recent_on_time_streak",
    "recent_5_late_rate_delta",
    "recent_5_avg_days_late_delta",
]

CATEGORICAL_FEATURES: list[str] = []

FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES


@dataclass(slots=True)
class PaymentRiskFeatures:
    amount: float
    payment_term: float

    prior_resolved_invoice_count: float
    prior_open_invoice_count: float
    open_invoice_ratio: float
    has_resolved_history: float

    historical_late_rate: float
    historical_avg_days_late: float

    historical_avg_invoice_amount: float
    amount_vs_historical_avg_ratio: float

    customer_tenure_days: float
    days_since_previous_invoice: float

    recent_5_late_rate: float
    recent_5_avg_days_late: float

    recent_late_streak: float
    recent_on_time_streak: float

    recent_5_late_rate_delta: float
    recent_5_avg_days_late_delta: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    def to_row(self) -> dict[str, float]:
        values = self.to_dict()
        return {
            feature: values[feature]
            for feature in FEATURES
        }


assert list(
    PaymentRiskFeatures.__dataclass_fields__
) == FEATURES
