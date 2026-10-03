from app.models.enums import PolicyDecisionResult, RecoveryActionType, RecoveryCaseStatus
from app.services.policy_engine import (
    ESCALATION_DAYS_THRESHOLD,
    HIGH_VALUE_THRESHOLD,
    MAX_EMAIL_REMINDERS,
    MIN_TIME_BETWEEN_REMINDERS_DAYS,
    RULE_BROKEN_PROMISE_FORCED_ESCALATE,
    RULE_COOLDOWN_NOT_ELAPSED,
    RULE_ESCALATED_SUPPRESSES_REMINDER,
    RULE_HIGH_VALUE_OVERDUE_FORCED_ESCALATE,
    RULE_HIGH_VALUE_REVIEW,
    RULE_NO_RESTRICTION,
    RULE_REMINDER_APPROVED,
    RULE_REMINDER_CAP_EXCEEDED,
    RULE_VOICE_CALL_BLOCKED_ESCALATED,
    evaluate_policy,
)


def test_first_reminder_is_approved():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.SEND_EMAIL,
        reminder_count=0,
        days_overdue=5,
        revenue_at_risk=100_000.0,
        case_status=RecoveryCaseStatus.OPEN,
        days_since_last_action=None,
    )
    assert outcome.final_action == RecoveryActionType.SEND_EMAIL
    assert outcome.decision == PolicyDecisionResult.APPROVED
    assert outcome.rule == RULE_REMINDER_APPROVED


def test_reminder_cap_forces_escalation():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.SEND_EMAIL,
        reminder_count=MAX_EMAIL_REMINDERS,
        days_overdue=30,
        revenue_at_risk=100_000.0,
        case_status=RecoveryCaseStatus.OPEN,
        days_since_last_action=10,
    )
    assert outcome.final_action == RecoveryActionType.ESCALATE
    assert outcome.decision == PolicyDecisionResult.REJECTED
    assert outcome.rule == RULE_REMINDER_CAP_EXCEEDED


def test_cooldown_not_elapsed_forces_wait():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.SEND_EMAIL,
        reminder_count=1,
        days_overdue=10,
        revenue_at_risk=100_000.0,
        case_status=RecoveryCaseStatus.OPEN,
        days_since_last_action=MIN_TIME_BETWEEN_REMINDERS_DAYS - 1,
    )
    assert outcome.final_action == RecoveryActionType.WAIT
    assert outcome.decision == PolicyDecisionResult.REJECTED
    assert outcome.rule == RULE_COOLDOWN_NOT_ELAPSED


def test_high_value_reminder_approved_but_flagged_for_review():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.SEND_PAYMENT_LINK,
        reminder_count=1,
        days_overdue=20,
        revenue_at_risk=HIGH_VALUE_THRESHOLD + 1,
        case_status=RecoveryCaseStatus.OPEN,
        days_since_last_action=MIN_TIME_BETWEEN_REMINDERS_DAYS,
    )
    assert outcome.final_action == RecoveryActionType.SEND_PAYMENT_LINK
    assert outcome.decision == PolicyDecisionResult.REQUIRES_HUMAN_REVIEW
    assert outcome.rule == RULE_HIGH_VALUE_REVIEW


def test_policy_constants_are_the_documented_business_rules():
    """Pins the thresholds the rest of this file reasons about.

    Every other test here passed its inputs as HIGH_VALUE_THRESHOLD or
    ESCALATION_DAYS_THRESHOLD imported from the module under test, so the
    inputs moved with the implementation and the rules were pinned to
    nothing: setting HIGH_VALUE_THRESHOLD to 1.0 left the forced-escalation
    test passing. Asserting the constants here means a deliberate change to
    a business rule has to be made deliberately, in one visible place,
    rather than silently invalidating the suite.
    """
    assert MAX_EMAIL_REMINDERS == 3
    assert MIN_TIME_BETWEEN_REMINDERS_DAYS == 7
    assert HIGH_VALUE_THRESHOLD == 1_000_000.0
    assert ESCALATION_DAYS_THRESHOLD == 45


def test_high_value_and_overdue_forces_escalation_even_on_first_cycle():
    # This is the Vertex scenario from the seed data: even a fresh
    # recommendation of SEND_EMAIL gets overridden the moment the case is
    # both high-value and past the escalation threshold.
    #
    # Inputs are LITERALS on purpose. Using the imported constants here
    # made the test follow the implementation instead of checking it; the
    # companion test above pins the constants themselves.
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.SEND_EMAIL,
        reminder_count=0,
        days_overdue=45,
        revenue_at_risk=1_000_000.0,
        case_status=RecoveryCaseStatus.OPEN,
        days_since_last_action=None,
    )
    assert outcome.final_action == RecoveryActionType.ESCALATE
    assert outcome.decision == PolicyDecisionResult.APPROVED
    assert "forced" in outcome.reason.lower()
    assert outcome.rule == RULE_HIGH_VALUE_OVERDUE_FORCED_ESCALATE


def test_broken_promise_forces_escalation_regardless_of_recommendation():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.WAIT,
        reminder_count=1,
        days_overdue=15,
        revenue_at_risk=100_000.0,
        case_status=RecoveryCaseStatus.MONITORING,
        days_since_last_action=3,
        has_broken_promise=True,
    )
    assert outcome.final_action == RecoveryActionType.ESCALATE
    assert outcome.decision == PolicyDecisionResult.APPROVED
    assert "promise" in outcome.reason.lower()
    assert outcome.rule == RULE_BROKEN_PROMISE_FORCED_ESCALATE


def test_escalated_case_suppresses_further_reminders():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.SEND_EMAIL,
        reminder_count=1,
        days_overdue=50,
        revenue_at_risk=100_000.0,
        case_status=RecoveryCaseStatus.ESCALATED,
        days_since_last_action=20,
    )
    assert outcome.final_action == RecoveryActionType.WAIT
    assert outcome.decision == PolicyDecisionResult.REJECTED
    assert outcome.rule == RULE_ESCALATED_SUPPRESSES_REMINDER


def test_promise_to_pay_and_wait_are_never_gated():
    # Deliberately below both HIGH_VALUE_THRESHOLD and ESCALATION_DAYS_THRESHOLD,
    # so the forced-escalation rule doesn't kick in and mask what this test checks.
    for action in (RecoveryActionType.TRACK_PROMISE_TO_PAY, RecoveryActionType.WAIT, RecoveryActionType.CLOSE_CASE):
        outcome = evaluate_policy(
            recommended_action=action,
            reminder_count=5,
            days_overdue=20,
            revenue_at_risk=100_000.0,
            case_status=RecoveryCaseStatus.OPEN,
            days_since_last_action=0,
        )
        assert outcome.final_action == action
        assert outcome.decision == PolicyDecisionResult.APPROVED
        assert outcome.rule == RULE_NO_RESTRICTION


def test_voice_call_blocked_on_escalated_case():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.PLACE_VOICE_CALL,
        reminder_count=2,
        days_overdue=50,
        revenue_at_risk=100_000.0,
        case_status=RecoveryCaseStatus.ESCALATED,
        days_since_last_action=10,
    )
    assert outcome.final_action == RecoveryActionType.WAIT
    assert outcome.decision == PolicyDecisionResult.REJECTED
    assert outcome.rule == RULE_VOICE_CALL_BLOCKED_ESCALATED


def test_voice_call_approved_on_non_escalated_case():
    outcome = evaluate_policy(
        recommended_action=RecoveryActionType.PLACE_VOICE_CALL,
        reminder_count=0,
        days_overdue=10,
        revenue_at_risk=100_000.0,
        case_status=RecoveryCaseStatus.OPEN,
        days_since_last_action=None,
    )
    assert outcome.final_action == RecoveryActionType.PLACE_VOICE_CALL
    assert outcome.decision == PolicyDecisionResult.APPROVED
    assert outcome.rule == RULE_NO_RESTRICTION
