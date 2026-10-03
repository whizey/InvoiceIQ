"""The recovery-risk model's feature contract.

FEATURE_NAMES defines both the column order XGBoost is trained on
(app/ml/synthetic_data.py) and the order real features are extracted in
(app/services/risk_context.py) — the two must never drift apart, which is
why both sides import from here rather than hardcoding column lists.
"""

from dataclasses import dataclass, fields

FEATURE_NAMES = [
    "invoice_amount",
    "days_overdue",
    "outstanding_balance",
    "avg_historical_payment_delay",
    "num_prior_late_payments",
    "num_prior_on_time_payments",
    "customer_tenure_days",
    "prior_recovery_success_rate",
    "num_open_invoices",
]


@dataclass
class RiskFeatures:
    invoice_amount: float
    days_overdue: int
    outstanding_balance: float
    avg_historical_payment_delay: float
    num_prior_late_payments: int
    num_prior_on_time_payments: int
    customer_tenure_days: int
    prior_recovery_success_rate: float
    num_open_invoices: int

    def to_row(self) -> dict:
        return {name: getattr(self, name) for name in FEATURE_NAMES}


assert [f.name for f in fields(RiskFeatures)] == FEATURE_NAMES, (
    "RiskFeatures fields must match FEATURE_NAMES order exactly"
)
