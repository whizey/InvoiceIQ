"""Offline evaluation of actual recommendations and policy rules."""

import asyncio
import json
import os
import sys

from pathlib import Path
from unittest.mock import patch

from app.agents import llm_client
from app.models.enums import (
    RecoveryActionType,
    RecoveryCaseStatus,
)
from app.services.policy_engine import evaluate_policy


# Synthetic evaluation fixtures, not actual customer data.
# id, overdue, amount, prior_late, prior_on_time,
# reminder_count, days_since_last, case_status, broken_promise,
# expected_recommendation, expected_final_action, expected_policy.

CASES = [
    (
        "first_reminder", 5, 150000, 0, 3, 0,
        None, "OPEN", False,
        "SEND_EMAIL", "SEND_EMAIL", "APPROVED",
    ),
    (
        "second_reminder", 14, 120000, 2, 1, 1,
        10, "OPEN", False,
        "SEND_PAYMENT_LINK", "SEND_PAYMENT_LINK", "APPROVED",
    ),
    (
        "escalation", 28, 90000, 3, 0, 3,
        10, "OPEN", False,
        "ESCALATE", "ESCALATE", "APPROVED",
    ),
    (
        "high_value", 50, 2000000, 0, 0, 0,
        None, "OPEN", False,
        "ESCALATE", "ESCALATE", "APPROVED",
    ),
    (
        "approval_gate", 10, 1100000, 0, 4, 0,
        None, "OPEN", False,
        "SEND_EMAIL", "SEND_EMAIL",
        "REQUIRES_HUMAN_REVIEW",
    ),
    (
        "cooldown", 10, 200000, 1, 3, 0,
        1, "OPEN", False,
        "SEND_EMAIL", "WAIT", "REJECTED",
    ),
    (
        "broken_promise", 12, 90000, 0, 3, 0,
        None, "OPEN", True,
        "SEND_EMAIL", "ESCALATE", "APPROVED",
    ),
    (
        "escalated_case", 12, 75000, 0, 3, 0,
        None, "ESCALATED", False,
        "SEND_EMAIL", "WAIT", "REJECTED",
    ),
]


async def evaluate():
    output = []

    with (
        patch.dict(
            os.environ,
            {
                "INVOICEIQ_PHASE7_ROUTING": "1",
                "INVOICEIQ_PHASE7_REMOTE": "0",
            },
        ),
        patch.object(
            llm_client,
            "_llm_chat",
            return_value=(None, None),
        ),
    ):
        for (
            name, days, amount, late, ontime,
            reminders, since_last, case_status,
            broken, exp_recommend, exp_action,
            exp_policy,
        ) in CASES:

            invoice = {
                "days_overdue": days,
                "amount_total": amount,
            }

            customer = {
                "num_prior_late_payments": late,
                "num_prior_on_time_payments": ontime,
                "promise_to_pay": (
                    {
                        "status": "BROKEN",
                        "promised_amount": 100,
                        "promised_date": "2026-01-01",
                    }
                    if broken else None
                ),
            }

            diagnosis, diag_model = (
                await llm_client.diagnose(
                    invoice,
                    customer,
                )
            )

            recommendation, rec_model = (
                await llm_client.recommend(
                    invoice,
                    customer,
                    diagnosis.model_dump(),
                    reminders,
                )
            )

            policy = evaluate_policy(
                recommended_action=RecoveryActionType(
                    recommendation.action
                ),
                reminder_count=reminders,
                days_overdue=days,
                revenue_at_risk=amount,
                case_status=RecoveryCaseStatus(
                    case_status
                ),
                days_since_last_action=since_last,
                has_broken_promise=broken,
            )

            checks = {
                "recommendation": (
                    recommendation.action
                    == exp_recommend
                ),
                "policy_action": (
                    policy.final_action.value
                    == exp_action
                ),
                "policy_decision": (
                    policy.decision.value
                    == exp_policy
                ),
                "no_remote_model": (
                    diag_model == rec_model
                    == llm_client.FALLBACK_MODEL_NAME
                ),
            }

            output.append({
                "scenario": name,
                "passed": all(checks.values()),
                "checks": checks,
            })

    return {
        "suite": "phase7-policy-v1",
        "offline": True,
        "total": len(output),
        "passed": sum(
            case["passed"] for case in output
        ),
        "results": output,
    }


if __name__ == "__main__":
    report = asyncio.run(evaluate())

    output = Path(
        "/private/tmp/invoiceiq-phase7-evaluation.json"
    )

    output.write_text(
        json.dumps(report, indent=2) + "\n"
    )

    print(json.dumps(report, indent=2))
    print("Evaluation report:", output)

    if report["passed"] != report["total"]:
        sys.exit(1)
