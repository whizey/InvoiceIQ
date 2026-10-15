"""Combined BPI survival scoring and recovery decision serving."""

from __future__ import annotations

import pandas as pd

from app.ml.bpi_survival_serving import (
    score_cases,
)
from app.services.bpi_recovery_policy import (
    decide_recovery_action,
)


def score_case_with_policy(
    case: pd.DataFrame,
    *,
    amount_outstanding: float,
    days_overdue: int,
    reminders_sent: int = 0,
    promise_to_pay_active: bool = False,
) -> dict:
    if len(case) != 1:
        raise ValueError(
            "score_case_with_policy expects exactly one invoice row."
        )

    scored = score_cases(
        case.reset_index(
            drop=True
        )
    )

    if scored.empty:
        raise RuntimeError(
            "BPI survival model returned no score."
        )

    survival = (
        scored.iloc[0]
        .to_dict()
    )

    decision = decide_recovery_action(
        clearance_probability_7d=(
            survival[
                "clearance_probability_7d"
            ]
        ),
        clearance_probability_30d=(
            survival[
                "clearance_probability_30d"
            ]
        ),
        clearance_probability_60d=(
            survival[
                "clearance_probability_60d"
            ]
        ),
        clearance_probability_90d=(
            survival[
                "clearance_probability_90d"
            ]
        ),
        amount_outstanding=(
            amount_outstanding
        ),
        days_overdue=(
            days_overdue
        ),
        reminders_sent=(
            reminders_sent
        ),
        promise_to_pay_active=(
            promise_to_pay_active
        ),
    )

    return {
        "survival": survival,
        "decision": (
            decision.to_dict()
        ),
    }
