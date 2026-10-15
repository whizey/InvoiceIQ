"""Deterministic recovery policy built on BPI survival predictions.

The survival model estimates when an invoice is likely to clear.
This module converts those probabilities into an operational decision.

The LLM/agent may explain the decision, but it does not invent the
risk tier or action from scratch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


RiskTier = Literal[
    "LOW",
    "MEDIUM",
    "HIGH",
]

RecoveryAction = Literal[
    "WAIT",
    "SEND_EMAIL",
    "SEND_PAYMENT_LINK",
    "PLACE_VOICE_CALL",
    "ESCALATE",
]


HIGH_VALUE_THRESHOLD = 1_000_000.0
FORCED_ESCALATION_DAYS = 45


@dataclass(frozen=True)
class RecoveryDecision:
    risk_tier: RiskTier
    urgency_score: float
    recommended_action: RecoveryAction
    human_review_required: bool
    policy_reasons: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _clip_probability(value: float) -> float:
    return max(
        0.0,
        min(
            1.0,
            float(value),
        ),
    )


def _validate_probability_curve(
    p7: float,
    p30: float,
    p60: float,
    p90: float,
) -> None:
    values = [
        p7,
        p30,
        p60,
        p90,
    ]

    if any(
        value < 0.0 or value > 1.0
        for value in values
    ):
        raise ValueError(
            "Clearance probabilities must be between 0 and 1."
        )

    if not (
        p7
        <= p30
        <= p60
        <= p90
    ):
        raise ValueError(
            "Clearance probabilities must be monotonic: "
            "P7 <= P30 <= P60 <= P90."
        )


def _urgency_score(
    *,
    p30: float,
    p60: float,
    days_overdue: int,
    amount_outstanding: float,
    reminders_sent: int,
) -> float:
    no_clear_30d = (
        1.0
        - p30
    )

    no_clear_60d = (
        1.0
        - p60
    )

    probability_component = (
        no_clear_30d
        * 55.0
        +
        no_clear_60d
        * 15.0
    )

    overdue_component = (
        min(
            max(
                days_overdue,
                0,
            ),
            60,
        )
        / 60.0
        * 20.0
    )

    amount_component = (
        10.0
        if amount_outstanding
        >= HIGH_VALUE_THRESHOLD
        else 0.0
    )

    reminder_component = min(
        max(
            reminders_sent,
            0,
        )
        * 2.5,
        5.0,
    )

    score = (
        probability_component
        + overdue_component
        + amount_component
        + reminder_component
    )

    return round(
        max(
            0.0,
            min(
                100.0,
                score,
            ),
        ),
        2,
    )


def _decide_recovery_action_candidate(
    *,
    clearance_probability_7d: float,
    clearance_probability_30d: float,
    clearance_probability_60d: float,
    clearance_probability_90d: float,
    amount_outstanding: float,
    days_overdue: int,
    reminders_sent: int = 0,
    promise_to_pay_active: bool = False,
) -> RecoveryDecision:
    p7 = _clip_probability(
        clearance_probability_7d
    )

    p30 = _clip_probability(
        clearance_probability_30d
    )

    p60 = _clip_probability(
        clearance_probability_60d
    )

    p90 = _clip_probability(
        clearance_probability_90d
    )

    _validate_probability_curve(
        p7,
        p30,
        p60,
        p90,
    )

    amount = max(
        0.0,
        float(
            amount_outstanding
        ),
    )

    overdue = max(
        0,
        int(
            days_overdue
        ),
    )

    reminders = max(
        0,
        int(
            reminders_sent
        ),
    )

    urgency = _urgency_score(
        p30=p30,
        p60=p60,
        days_overdue=overdue,
        amount_outstanding=amount,
        reminders_sent=reminders,
    )

    reasons: list[str] = []

    if promise_to_pay_active:
        reasons.append(
            "Active promise-to-pay exists; avoid unnecessary escalation."
        )

        return RecoveryDecision(
            risk_tier="LOW",
            urgency_score=urgency,
            recommended_action="WAIT",
            human_review_required=False,
            policy_reasons=reasons,
        )

    if (
        amount
        >= HIGH_VALUE_THRESHOLD
        and overdue
        >= FORCED_ESCALATION_DAYS
    ):
        reasons.extend(
            [
                (
                    f"Invoice is {overdue} days overdue, "
                    f"meeting the {FORCED_ESCALATION_DAYS}-day "
                    "escalation threshold."
                ),
                (
                    "Outstanding amount exceeds the "
                    "high-value review threshold."
                ),
                (
                    f"30-day clearance probability is {p30:.1%}."
                ),
            ]
        )

        return RecoveryDecision(
            risk_tier="HIGH",
            urgency_score=max(
                urgency,
                85.0,
            ),
            recommended_action="ESCALATE",
            human_review_required=True,
            policy_reasons=reasons,
        )

    if (
        p30 < 0.35
        or (
            p60 < 0.55
            and overdue >= 30
        )
    ):
        reasons.append(
            (
                "Survival model indicates a low probability "
                "of near-term clearance."
            )
        )

        if overdue >= 30:
            reasons.append(
                f"Invoice is already {overdue} days overdue."
            )

        if reminders >= 2:
            reasons.append(
                (
                    f"{reminders} previous reminders have "
                    "already been sent."
                )
            )

        if (
            overdue >= 30
            or reminders >= 2
        ):
            action: RecoveryAction = (
                "ESCALATE"
            )
        else:
            action = (
                "PLACE_VOICE_CALL"
            )

        return RecoveryDecision(
            risk_tier="HIGH",
            urgency_score=max(
                urgency,
                70.0,
            ),
            recommended_action=action,
            human_review_required=(
                action == "ESCALATE"
                or amount
                >= HIGH_VALUE_THRESHOLD
            ),
            policy_reasons=reasons,
        )

    if (
        p7 < 0.25
        and p30 < 0.70
    ):
        reasons.append(
            (
                f"Only {p7:.1%} probability of clearing "
                "within 7 days."
            )
        )

        reasons.append(
            (
                f"30-day clearance probability is {p30:.1%}."
            )
        )

        if (
            overdue >= 15
            or reminders >= 1
        ):
            action = (
                "SEND_PAYMENT_LINK"
            )

            reasons.append(
                (
                    "Invoice has remained unresolved long "
                    "enough to justify a stronger intervention."
                )
            )
        else:
            action = (
                "SEND_EMAIL"
            )

        return RecoveryDecision(
            risk_tier="MEDIUM",
            urgency_score=max(
                urgency,
                40.0,
            ),
            recommended_action=action,
            human_review_required=(
                amount
                >= HIGH_VALUE_THRESHOLD
            ),
            policy_reasons=reasons,
        )

    if p7 < 0.55:
        reasons.extend(
            [
                (
                    f"7-day clearance probability is {p7:.1%}, "
                    "so passive monitoring alone may be too weak."
                ),
                (
                    f"30-day clearance probability remains "
                    f"{p30:.1%}."
                ),
            ]
        )

        return RecoveryDecision(
            risk_tier="MEDIUM",
            urgency_score=max(
                urgency,
                30.0,
            ),
            recommended_action="SEND_EMAIL",
            human_review_required=False,
            policy_reasons=reasons,
        )

    reasons.extend(
        [
            (
                f"Near-term clearance probability is strong: "
                f"{p7:.1%} within 7 days."
            ),
            (
                f"30-day clearance probability is {p30:.1%}."
            ),
            (
                "No intervention threshold triggered."
            ),
        ]
    )

    return RecoveryDecision(
        risk_tier="LOW",
        urgency_score=min(
            urgency,
            39.99,
        ),
        recommended_action="WAIT",
        human_review_required=False,
        policy_reasons=reasons,
    )


def decide_recovery_action(
    *,
    clearance_probability_7d: float,
    clearance_probability_30d: float,
    clearance_probability_60d: float,
    clearance_probability_90d: float,
    amount_outstanding: float,
    days_overdue: int,
    reminders_sent: int = 0,
    promise_to_pay_active: bool = False,
) -> RecoveryDecision:
    """Separate model risk from permission to take an action."""

    candidate = _decide_recovery_action_candidate(
        clearance_probability_7d=clearance_probability_7d,
        clearance_probability_30d=clearance_probability_30d,
        clearance_probability_60d=clearance_probability_60d,
        clearance_probability_90d=clearance_probability_90d,
        amount_outstanding=amount_outstanding,
        days_overdue=days_overdue,
        reminders_sent=reminders_sent,
        promise_to_pay_active=False,
    )

    amount = max(0.0, float(amount_outstanding))
    overdue = max(0, int(days_overdue))
    reminders = max(0, int(reminders_sent))

    action = str(
        getattr(
            candidate.recommended_action,
            "value",
            candidate.recommended_action,
        )
    )

    reasons = list(candidate.policy_reasons)

    if amount <= 0:
        action = "WAIT"
        reasons.append(
            "No outstanding balance; recovery actions are blocked."
        )

    elif overdue <= 0:
        action = "WAIT"
        reasons.append(
            "Invoice is not overdue; recovery contact is blocked."
        )

    elif promise_to_pay_active:
        action = "WAIT"
        reasons.append(
            "Active promise-to-pay; intervention is paused "
            "without changing the predicted risk tier."
        )

    elif action == "PLACE_VOICE_CALL":
        if overdue < 14 or reminders < 1:
            action = "SEND_EMAIL"
            reasons.append(
                "Voice call blocked until the invoice is at least "
                "14 days overdue and one reminder has been sent."
            )

    review_required = (
        action in {"PLACE_VOICE_CALL", "ESCALATE"}
        or (
            action != "WAIT"
            and bool(candidate.human_review_required)
        )
    )

    return RecoveryDecision(
        risk_tier=candidate.risk_tier,
        urgency_score=candidate.urgency_score,
        recommended_action=action,
        human_review_required=review_required,
        policy_reasons=reasons,
    )
