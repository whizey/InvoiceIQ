from app.services.bpi_recovery_policy import (
    FORCED_ESCALATION_DAYS,
    HIGH_VALUE_THRESHOLD,
    decide_recovery_action,
)


def decide(**overrides):
    values = {
        "clearance_probability_7d": 0.02,
        "clearance_probability_30d": 0.10,
        "clearance_probability_60d": 0.40,
        "clearance_probability_90d": 0.80,
        "amount_outstanding": 10000.0,
        "days_overdue": 20,
        "reminders_sent": 0,
        "promise_to_pay_active": False,
    }
    values.update(overrides)
    return decide_recovery_action(**values)


def test_invoice_not_overdue():
    result = decide(days_overdue=0)
    assert result.risk_tier == "HIGH"
    assert result.recommended_action == "WAIT"
    assert not result.human_review_required


def test_zero_outstanding_balance():
    result = decide(amount_outstanding=0.0)
    assert result.recommended_action == "WAIT"
    assert not result.human_review_required


def test_early_overdue_uses_email():
    result = decide(days_overdue=2)
    assert result.recommended_action == "SEND_EMAIL"


def test_voice_requires_prior_reminder():
    result = decide(days_overdue=20, reminders_sent=1)
    assert result.recommended_action == "PLACE_VOICE_CALL"
    assert result.human_review_required


def test_voice_blocked_without_reminder():
    result = decide(days_overdue=20, reminders_sent=0)
    assert result.recommended_action == "SEND_EMAIL"


def test_active_promise_keeps_risk():
    result = decide(
        days_overdue=20,
        reminders_sent=1,
        promise_to_pay_active=True,
    )
    assert result.risk_tier == "HIGH"
    assert result.recommended_action == "WAIT"
    assert not result.human_review_required


def test_high_value_escalation():
    result = decide(
        amount_outstanding=float(HIGH_VALUE_THRESHOLD),
        days_overdue=int(FORCED_ESCALATION_DAYS),
    )
    assert result.recommended_action == "ESCALATE"
    assert result.human_review_required
