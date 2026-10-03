import json
from pathlib import Path

from app.ml.features import RiskFeatures
from app.ml.risk_model import score
from app.models.enums import RiskLevel


def _good_features() -> RiskFeatures:
    """A reliable, established, low-value, barely-overdue customer."""
    return RiskFeatures(
        invoice_amount=100_000.0,
        days_overdue=3,
        outstanding_balance=100_000.0,
        avg_historical_payment_delay=-1.0,
        num_prior_late_payments=0,
        num_prior_on_time_payments=8,
        customer_tenure_days=900,
        prior_recovery_success_rate=0.95,
        num_open_invoices=0,
    )


def _bad_features() -> RiskFeatures:
    """A new, high-value, badly overdue customer with a poor track record."""
    return RiskFeatures(
        invoice_amount=2_500_000.0,
        days_overdue=100,
        outstanding_balance=2_500_000.0,
        avg_historical_payment_delay=25.0,
        num_prior_late_payments=6,
        num_prior_on_time_payments=0,
        customer_tenure_days=60,
        prior_recovery_success_rate=0.1,
        num_open_invoices=4,
    )


def test_model_scores_are_well_formed():
    result = score(_good_features())
    assert 0 <= result.risk_score <= 100
    assert 0 <= result.recovery_probability <= 1
    assert isinstance(result.risk_level, RiskLevel)


def test_model_ranks_bad_case_riskier_than_good_case():
    good = score(_good_features())
    bad = score(_bad_features())

    assert bad.risk_score > good.risk_score
    assert bad.recovery_probability < good.recovery_probability
    assert good.risk_level == RiskLevel.LOW
    assert bad.risk_level == RiskLevel.HIGH


def test_metrics_file_shows_real_discriminative_power():
    metrics_path = Path(__file__).parent.parent / "app" / "ml" / "artifacts" / "metrics.json"
    metrics = json.loads(metrics_path.read_text())

    # AUC well above 0.5 (random) proves the model learned the synthetic
    # signal rather than just memorizing noise.
    assert metrics["auc"] > 0.7
    assert "not a production financial model" in metrics["note"]
