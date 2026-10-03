from app.ml.features import FEATURE_NAMES, RiskFeatures
from app.ml.risk_model import explain


def sample_features() -> RiskFeatures:
    return RiskFeatures(
        invoice_amount=450000,
        days_overdue=30,
        outstanding_balance=450000,
        avg_historical_payment_delay=8.0,
        num_prior_late_payments=3,
        num_prior_on_time_payments=1,
        customer_tenure_days=500,
        prior_recovery_success_rate=0.5,
        num_open_invoices=2,
    )


def test_explanation_contains_every_feature():
    result = explain(sample_features())

    assert 0 <= result.recovery_probability <= 1
    assert 0 <= result.risk_score <= 100
    assert len(result.contributions) == len(FEATURE_NAMES)

    explained = {item.feature for item in result.contributions}

    assert explained == set(FEATURE_NAMES)


def test_explanation_sorted_by_absolute_importance():
    result = explain(sample_features())

    values = [
        abs(item.shap_value)
        for item in result.contributions
    ]

    assert values == sorted(values, reverse=True)


def test_explanation_has_readable_labels():
    result = explain(sample_features())

    for item in result.contributions:
        assert item.label
        assert "_" not in item.label