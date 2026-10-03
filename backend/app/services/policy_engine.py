"""Deterministic policy engine — the only layer allowed to gate execution.

The LLM (app/agents/) recommends an action; this module decides whether it
actually happens, and can override it outright. Every decision is
auditable (PolicyDecision rows) and nothing here depends on prompting a
model to "be careful" — it's plain Python against configured thresholds.

TODO(Phase 10): make these thresholds configurable (env/DB) instead of
module constants, and add per-segment overrides.
"""

from dataclasses import dataclass

from app.models.enums import PolicyDecisionResult, RecoveryActionType, RecoveryCaseStatus

MAX_EMAIL_REMINDERS = 3
MIN_TIME_BETWEEN_REMINDERS_DAYS = 7
HIGH_VALUE_THRESHOLD = 1_000_000.0
ESCALATION_DAYS_THRESHOLD = 45

_REMINDER_ACTIONS = (RecoveryActionType.SEND_EMAIL, RecoveryActionType.SEND_PAYMENT_LINK)

# Machine-readable tags for each branch below, persisted on PolicyDecision.rule
# so live cycles and hand-seeded demo data (app/seed/run.py) can be aggregated
# together for the AI-oversight dashboard (app/services/metrics.py). Keep
# these in sync with seed/run.py's hand-authored rows — both must use the
# exact same strings for the rollup to be meaningful.
RULE_BROKEN_PROMISE_FORCED_ESCALATE = "broken_promise_forced_escalate"
RULE_HIGH_VALUE_OVERDUE_FORCED_ESCALATE = "high_value_overdue_forced_escalate"
RULE_ESCALATED_SUPPRESSES_REMINDER = "escalated_suppresses_reminder"
RULE_REMINDER_CAP_EXCEEDED = "reminder_cap_exceeded"
RULE_COOLDOWN_NOT_ELAPSED = "cooldown_not_elapsed"
RULE_HIGH_VALUE_REVIEW = "high_value_review"
RULE_REMINDER_APPROVED = "reminder_approved"
RULE_NO_RESTRICTION = "no_restriction"
RULE_VOICE_CALL_BLOCKED_ESCALATED = "voice_call_blocked_escalated"


@dataclass
class PolicyOutcome:
    final_action: RecoveryActionType
    decision: PolicyDecisionResult
    reason: str
    rule: str


def evaluate_policy(
    *,
    recommended_action: RecoveryActionType,
    reminder_count: int,
    days_overdue: int,
    revenue_at_risk: float,
    case_status: RecoveryCaseStatus,
    days_since_last_action: int | None,
    has_broken_promise: bool = False,
) -> PolicyOutcome:
    # A broken commitment is a hard fact, not something left to the LLM to
    # notice — force escalation regardless of what was recommended.
    if has_broken_promise:
        return PolicyOutcome(
            final_action=RecoveryActionType.ESCALATE,
            decision=PolicyDecisionResult.APPROVED,
            reason="Customer did not honor a promised payment date; escalation forced regardless of AI recommendation.",
            rule=RULE_BROKEN_PROMISE_FORCED_ESCALATE,
        )

    # Forced escalation: high value + significantly overdue overrides everything,
    # including a recommendation to WAIT or send another reminder.
    if revenue_at_risk >= HIGH_VALUE_THRESHOLD and days_overdue >= ESCALATION_DAYS_THRESHOLD:
        return PolicyOutcome(
            final_action=RecoveryActionType.ESCALATE,
            decision=PolicyDecisionResult.APPROVED,
            reason=(
                f"Amount (₹{revenue_at_risk:,.2f}) exceeds HIGH_VALUE_THRESHOLD "
                f"(₹{HIGH_VALUE_THRESHOLD:,.2f}) and days overdue ({days_overdue}) exceeds "
                f"ESCALATION_THRESHOLD ({ESCALATION_DAYS_THRESHOLD}); escalation forced "
                f"regardless of AI recommendation."
            ),
            rule=RULE_HIGH_VALUE_OVERDUE_FORCED_ESCALATE,
        )

    # Once escalated, a human has taken over — automated reminders stop.
    if case_status == RecoveryCaseStatus.ESCALATED and recommended_action in _REMINDER_ACTIONS:
        return PolicyOutcome(
            final_action=RecoveryActionType.WAIT,
            decision=PolicyDecisionResult.REJECTED,
            reason="Case is already escalated to a human; automated reminders are suppressed.",
            rule=RULE_ESCALATED_SUPPRESSES_REMINDER,
        )

    # A human has already taken over an escalated case — no new automated
    # or agent-driven voice call should start on top of that.
    if recommended_action == RecoveryActionType.PLACE_VOICE_CALL and case_status == RecoveryCaseStatus.ESCALATED:
        return PolicyOutcome(
            final_action=RecoveryActionType.WAIT,
            decision=PolicyDecisionResult.REJECTED,
            reason="Case is already escalated to a human; a voice call cannot be started.",
            rule=RULE_VOICE_CALL_BLOCKED_ESCALATED,
        )

    if recommended_action in _REMINDER_ACTIONS:
        if reminder_count >= MAX_EMAIL_REMINDERS:
            return PolicyOutcome(
                final_action=RecoveryActionType.ESCALATE,
                decision=PolicyDecisionResult.REJECTED,
                reason=(
                    f"Reminder count ({reminder_count}) has reached MAX_EMAIL_REMINDERS "
                    f"({MAX_EMAIL_REMINDERS}); escalating instead."
                ),
                rule=RULE_REMINDER_CAP_EXCEEDED,
            )
        if days_since_last_action is not None and days_since_last_action < MIN_TIME_BETWEEN_REMINDERS_DAYS:
            return PolicyOutcome(
                final_action=RecoveryActionType.WAIT,
                decision=PolicyDecisionResult.REJECTED,
                reason=(
                    f"Only {days_since_last_action} day(s) since the last reminder; "
                    f"MIN_TIME_BETWEEN_REMINDERS_DAYS ({MIN_TIME_BETWEEN_REMINDERS_DAYS}) not yet elapsed."
                ),
                rule=RULE_COOLDOWN_NOT_ELAPSED,
            )
        if revenue_at_risk >= HIGH_VALUE_THRESHOLD:
            # ADVISORY, NOT A GATE -- and worth being explicit, because the
            # enum name reads like a gate and nothing in the backend
            # branches on this value (only a frontend badge does). The
            # action below still executes immediately. That is the intended
            # design: the seeded narrative in app/seed/run.py describes it
            # as "action logged, human review flagged in parallel", and a
            # reminder email is reversible enough not to warrant blocking.
            #
            # If this ever needs to actually block, execute_action_node in
            # app/agents/graph.py is the place to branch, and
            # tests/test_policy_engine.py asserts the current contract.
            # Do not change the name alone and leave the behaviour: a
            # decision called REQUIRES_HUMAN_REVIEW that silently proceeds
            # is the kind of thing a reviewer is right to distrust.
            return PolicyOutcome(
                final_action=recommended_action,
                decision=PolicyDecisionResult.REQUIRES_HUMAN_REVIEW,
                reason=(
                    f"Amount (₹{revenue_at_risk:,.2f}) exceeds HIGH_VALUE_THRESHOLD; "
                    f"action approved but flagged for human review."
                ),
                rule=RULE_HIGH_VALUE_REVIEW,
            )
        return PolicyOutcome(
            final_action=recommended_action,
            decision=PolicyDecisionResult.APPROVED,
            reason=f"Reminder count ({reminder_count}) below MAX_EMAIL_REMINDERS ({MAX_EMAIL_REMINDERS}).",
            rule=RULE_REMINDER_APPROVED,
        )

    # TRACK_PROMISE_TO_PAY, ESCALATE, WAIT, CLOSE_CASE: no additional gating in V1.
    return PolicyOutcome(
        final_action=recommended_action,
        decision=PolicyDecisionResult.APPROVED,
        reason=f"No policy restriction applies to {recommended_action.value}.",
        rule=RULE_NO_RESTRICTION,
    )
