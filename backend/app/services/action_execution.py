"""Persist tool outcomes without treating failures or simulations as recovery."""
from datetime import datetime, timezone

from app.models import AuditLog
from app.models.enums import AuditActor, RecoveryActionStatus


async def execute_authorized_action(session, action, case, invoice, contact, *, executor, human_authorized=False):
    if invoice.company_id != case.company_id or invoice.id != case.invoice_id:
        raise PermissionError("Action invoice is outside the recovery case")
    if action.recovery_case_id != case.id or action.status not in {
        RecoveryActionStatus.POLICY_APPROVED, RecoveryActionStatus.PENDING_APPROVAL,
    }:
        raise PermissionError("Action is not authorized for execution")
    if action.status == RecoveryActionStatus.PENDING_APPROVAL and not human_authorized:
        raise PermissionError("Human approval is required")
    # The caller holds the action/case locks and has checked policy or reviewer
    # authorization. Roll back partial tool writes without losing its audit trail.
    try:
        async with session.begin_nested():
            result = await executor(session, action.action_type, case, invoice, contact)
            if not isinstance(result, dict) or result.get("success") is False or result.get("error"):
                raise RuntimeError("Action did not succeed")
    except Exception:
        result = {"success": False, "error": "action_failed", "outcome_verified": False}
        action.status = RecoveryActionStatus.FAILED
        action.executed_at = None
        session.add(AuditLog(
            recovery_case_id=case.id, entity_type="recovery_action", entity_id=action.id,
            event_type="ACTION_FAILED", actor=AuditActor.SYSTEM,
            description="Authorized action failed. Investigate before proposing a new action.",
            occurred_at=datetime.now(timezone.utc),
        ))
    else:
        result = {**result, "success": True, "outcome_verified": False}
        action.status = RecoveryActionStatus.EXECUTED
        action.executed_at = datetime.now(timezone.utc)
    action.result = result
    await session.flush()
    return result
