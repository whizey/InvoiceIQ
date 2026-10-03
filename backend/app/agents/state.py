"""Typed state for the recovery workflow graph.

Deliberately holds only IDs and plain data (no ORM objects) — every node
re-fetches what it needs from the DB via the IDs here, so nodes don't have
to trust another node's object freshness, and the state stays inspectable/
loggable as plain JSON.
"""

from typing import TypedDict


class RecoveryState(TypedDict, total=False):
    case_id: str
    terminal: bool
    awaiting_review: bool
    risk_drivers: list

    invoice_context: dict
    customer_context: dict

    risk_score: float
    risk_level: str
    recovery_probability: float

    diagnosis: dict

    reminder_count: int
    recommended_action: str
    recommendation_rationale: str

    action_id: str
    final_action: str
    policy_decision: str
    policy_reason: str

    action_result: dict

    case_status: str
    outcome_summary: str
