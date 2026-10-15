import pytest

from app.services.bpi_recovery_policy import (
    decide_recovery_action,
)


def test_high_probability_invoice_waits():
    result = decide_recovery_action(
        clearance_probability_7d=0.82,
        clearance_probability_30d=0.94,
        clearance_probability_60d=0.98,
        clearance_probability_90d=0.99,
        amount_outstanding=150000,
        days_overdue=3,
    )

    assert result.risk_tier == "LOW"
    assert result.recommended_action == "WAIT"
    assert result.human_review_required is False


def test_medium_risk_invoice_gets_email():
    result = decide_recovery_action(
        clearance_probability_7d=0.40,
        clearance_probability_30d=0.78,
        clearance_probability_60d=0.90,
        clearance_probability_90d=0.96,
        amount_outstanding=250000,
        days_overdue=6,
    )

    assert result.risk_tier == "MEDIUM"
    assert result.recommended_action == "SEND_EMAIL"


def test_medium_risk_after_reminder_gets_payment_link():
    result = decide_recovery_action(
        clearance_probability_7d=0.18,
        clearance_probability_30d=0.62,
        clearance_probability_60d=0.82,
        clearance_probability_90d=0.93,
        amount_outstanding=400000,
        days_overdue=18,
        reminders_sent=1,
    )

    assert result.risk_tier == "MEDIUM"
    assert (
        result.recommended_action
        == "SEND_PAYMENT_LINK"
    )


def test_low_recovery_probability_escalates():
    result = decide_recovery_action(
        clearance_probability_7d=0.08,
        clearance_probability_30d=0.28,
        clearance_probability_60d=0.48,
        clearance_probability_90d=0.71,
        amount_outstanding=600000,
        days_overdue=35,
        reminders_sent=2,
    )

    assert result.risk_tier == "HIGH"
    assert result.recommended_action == "ESCALATE"
    assert result.human_review_required is True


def test_high_value_old_invoice_forces_escalation():
    result = decide_recovery_action(
        clearance_probability_7d=0.55,
        clearance_probability_30d=0.75,
        clearance_probability_60d=0.88,
        clearance_probability_90d=0.95,
        amount_outstanding=1800000,
        days_overdue=50,
        reminders_sent=1,
    )

    assert result.risk_tier == "HIGH"
    assert result.recommended_action == "ESCALATE"
    assert result.human_review_required is True
    assert result.urgency_score >= 85.0


def test_active_promise_to_pay_waits():
    result = decide_recovery_action(
        clearance_probability_7d=0.10,
        clearance_probability_30d=0.30,
        clearance_probability_60d=0.50,
        clearance_probability_90d=0.75,
        amount_outstanding=300000,
        days_overdue=20,
        reminders_sent=2,
        promise_to_pay_active=True,
    )

    assert result.recommended_action == "WAIT"


def test_invalid_non_monotonic_curve_rejected():
    with pytest.raises(
        ValueError,
        match="monotonic",
    ):
        decide_recovery_action(
            clearance_probability_7d=0.70,
            clearance_probability_30d=0.60,
            clearance_probability_60d=0.80,
            clearance_probability_90d=0.90,
            amount_outstanding=100000,
            days_overdue=5,
        )


def test_lower_clearance_probability_increases_urgency():
    safer = decide_recovery_action(
        clearance_probability_7d=0.60,
        clearance_probability_30d=0.85,
        clearance_probability_60d=0.94,
        clearance_probability_90d=0.98,
        amount_outstanding=200000,
        days_overdue=10,
    )

    riskier = decide_recovery_action(
        clearance_probability_7d=0.30,
        clearance_probability_30d=0.55,
        clearance_probability_60d=0.75,
        clearance_probability_90d=0.90,
        amount_outstanding=200000,
        days_overdue=10,
    )

    assert (
        riskier.urgency_score
        > safer.urgency_score
    )
