"""Typed state for the recovery workflow graph.

Deliberately holds only IDs and plain data (no ORM objects) — every node
re-fetches what it needs from the DB via the IDs here, so nodes don't have
to trust another node's object freshness, and the state stays inspectable/
loggable as plain JSON.
"""

from typing import TypedDict


class RecoveryState(TypedDict, total=False):
    investigation_id: str
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
    knowledge_evidence: list[dict]

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


# Validate the complete merged state at each boundary, including node outputs.
# Required predecessor fields make a missing transition fail before tools run.
from typing_extensions import TypedDict as ValidatedTypedDict  # noqa: E402
from pydantic import ConfigDict, TypeAdapter  # noqa: E402
from typing import Literal  # noqa: E402


class StateContract(ValidatedTypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)
    investigation_id: str
    case_id: str
    terminal: bool
    awaiting_review: bool
    risk_drivers: list
    invoice_context: dict
    customer_context: dict
    risk_score: float | None
    risk_level: str | None
    recovery_probability: float | None
    diagnosis: dict
    knowledge_evidence: list[dict]
    reminder_count: int
    recommended_action: Literal["SEND_EMAIL", "SEND_PAYMENT_LINK", "TRACK_PROMISE_TO_PAY", "ESCALATE", "WAIT", "CLOSE_CASE"]
    recommendation_rationale: str
    action_id: str
    final_action: Literal["SEND_EMAIL", "SEND_PAYMENT_LINK", "TRACK_PROMISE_TO_PAY", "ESCALATE", "WAIT", "CLOSE_CASE"]
    policy_decision: Literal["APPROVED", "REJECTED", "REQUIRES_HUMAN_REVIEW"]
    policy_reason: str
    action_result: dict
    case_status: str
    outcome_summary: str


_STATE = TypeAdapter(StateContract)
_REQUIRED = {
    "check_terminal": ("case_id",),
    "load_customer_context": ("case_id", "terminal"),
    "calculate_recovery_risk": ("case_id", "invoice_context", "customer_context"),
    "diagnose_case": ("case_id", "invoice_context", "customer_context"),
    "recommend_intervention": ("case_id", "invoice_context", "customer_context", "diagnosis"),
    "policy_check": ("case_id", "invoice_context", "recommended_action", "reminder_count"),
    "execute_action": ("case_id", "action_id", "final_action", "policy_decision"),
    "record_outcome": ("case_id", "final_action", "action_result"),
}


def validate_transition(node: str, state: dict) -> dict:
    from uuid import UUID
    validated = _STATE.validate_python(state)
    for field in _REQUIRED[node]:
        if field not in validated:
            raise ValueError("Recovery transition is missing required state")
    for field in ("case_id", "investigation_id", "action_id"):
        if field in validated:
            UUID(validated[field])
    if "diagnosis" in validated:
        from app.agents.schemas import DiagnosisResult
        DiagnosisResult.model_validate(validated["diagnosis"])
    if validated.get("reminder_count", 0) < 0:
        raise ValueError("Reminder count cannot be negative")
    return validated


_OUTPUT_REQUIRED = {
    "check_terminal": ("terminal",),
    "load_customer_context": ("invoice_context", "customer_context"),
    "calculate_recovery_risk": ("risk_score", "risk_level", "recovery_probability", "risk_drivers"),
    "diagnose_case": ("diagnosis", "knowledge_evidence"),
    "recommend_intervention": ("recommended_action", "recommendation_rationale", "reminder_count"),
    "policy_check": ("action_id", "final_action", "policy_decision", "policy_reason"),
    "execute_action": ("action_result",),
    "record_outcome": ("case_status", "outcome_summary"),
}


def validate_node_output(node: str, state: dict, result: dict) -> None:
    if not isinstance(result, dict) or any(key not in result for key in _OUTPUT_REQUIRED[node]):
        raise ValueError("Recovery node returned incomplete output")
    if "case_id" in result or "investigation_id" in result:
        raise ValueError("Recovery node cannot replace investigation identity")
    validate_transition(node, {**state, **result})
