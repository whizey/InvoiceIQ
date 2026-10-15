"""Input trust boundary and output validation for the recovery/voice agents.

Heuristic prompt-injection screening is defense in depth, not a complete
prompt-injection solution. The deterministic policy engine and action-level
checks remain the security boundary, regardless of model output.
"""

import re
from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_SUSPICIOUS = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior|system|developer)\s+(?:instructions|rules|messages)"
    r"|reveal\s+(?:your\s+)?(?:system\s+prompt|api\s+key|secret)"
    r"|(?:system|developer)\s+(?:message|instructions)\s*:"
    r"|<\|(?:im_start|im_end)\|>"
    r"|\b(?:you\s+are\s+now|override\s+(?:the\s+)?(?:policy|approval))\b)",
    flags=re.IGNORECASE,
)

MAX_CONTEXT_CHARS = 24_000
MAX_MODEL_RESPONSE_CHARS = 4_000


def _untrusted_strings(value: object, *, depth: int = 0):
    if depth > 8:
        yield "depth exceeded " * 100
    elif isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _untrusted_strings(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _untrusted_strings(item, depth=depth + 1)


def is_untrusted_instruction(value: object) -> bool:
    """Prefer a deterministic fallback for overtly hostile or oversized input."""
    total = 0
    for content in _untrusted_strings(value):
        total += len(content)
        if total > MAX_CONTEXT_CHARS or _SUSPICIOUS.search(content):
            return True
    return False


def validate_model_output(output: object, schema: type[T]) -> T | None:
    """Validate actual data, including mocked providers that bypass LangChain.

    Never accept new action names, extra tool-call fields, or unbounded text.
    """
    if isinstance(output, schema):
        result = output
    elif isinstance(output, dict) and set(output).issubset(schema.model_fields):
        try:
            result = schema.model_validate(output)
        except ValidationError:
            return None
    else:
        return None

    if len(result.model_dump_json()) > MAX_MODEL_RESPONSE_CHARS:
        return None
    return result


def assert_action_can_execute(action: object, case_id: object) -> None:
    """Last-mile guard before any mocked/external recovery action runs.

    Rejected recommendations may be substituted by the deterministic policy
    into WAIT or ESCALATE. They must never execute their rejected original
    action. Human-review actions are always blocked here until separately
    approved through the authenticated review gate.
    """
    from app.models.enums import RecoveryActionStatus, RecoveryActionType

    if action is None or getattr(action, "recovery_case_id", None) != case_id:
        raise PermissionError("Action does not belong to this recovery case")
    if action.status == RecoveryActionStatus.POLICY_APPROVED:
        return
    if (
        action.status == RecoveryActionStatus.POLICY_REJECTED
        and action.action_type in {RecoveryActionType.WAIT, RecoveryActionType.ESCALATE}
        and action.action_type != action.recommended_action_type
    ):
        return
    raise PermissionError("Action has no executable policy authorization")
