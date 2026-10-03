"""Human approval gate for actions the policy engine holds.

The policy engine returns REQUIRES_HUMAN_REVIEW for high-value reminders.
graph.policy_check_node stores those as PENDING_APPROVAL and the workflow
stops. Nothing is sent until approve() runs; reject() records the refusal.
Both write an AuditLog row with actor HUMAN.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import get_publisher, topics
from app.models import AuditLog, Invoice, RecoveryAction, RecoveryCase
from app.models.enums import AuditActor, RecoveryActionStatus
from app.services.action_policy import primary_contact
from app.tools.mock_tools import execute_mock_action


class ReviewError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _load_pending(session: AsyncSession, action_id: UUID) -> RecoveryAction:
    action = (
        await session.execute(
            select(RecoveryAction).where(RecoveryAction.id == action_id).with_for_update()
        )
    ).scalar_one_or_none()
    if action is None:
        raise ReviewError("Action not found", 404)
    if action.status != RecoveryActionStatus.PENDING_APPROVAL:
        raise ReviewError(f"Action is {action.status.value}, not waiting for approval")
    return action


async def list_pending(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(
            select(RecoveryAction)
            .where(RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL)
            .order_by(RecoveryAction.created_at)
        )
    ).scalars().all()
    return [
        {
            "action_id": str(a.id),
            "case_id": str(a.recovery_case_id),
            "action_type": a.action_type.value,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


async def approve(session: AsyncSession, action_id: UUID, reviewer: str, note: str | None) -> dict:
    action = await _load_pending(session, action_id)
    case = await session.get(RecoveryCase, action.recovery_case_id)
    invoice = await session.get(Invoice, case.invoice_id)
    contact = await primary_contact(session, case.company_id)

    result = await execute_mock_action(session, action.action_type, case, invoice, contact)
    action.status = RecoveryActionStatus.EXECUTED
    action.executed_at = _now()
    action.result = result
    session.add(
        AuditLog(
            recovery_case_id=case.id,
            entity_type="recovery_action",
            entity_id=action.id,
            event_type="REVIEW_APPROVED",
            actor=AuditActor.HUMAN,
            description=f"{reviewer} approved {action.action_type.value}. {note or ''}".strip(),
            occurred_at=_now(),
        )
    )
    await session.flush()
    return {
        "action_id": str(action.id),
        "case_id": str(case.id),
        "action_type": action.action_type.value,
        "decision": "APPROVED",
        "reviewer": reviewer,
    }


async def reject(session: AsyncSession, action_id: UUID, reviewer: str, note: str | None) -> dict:
    action = await _load_pending(session, action_id)
    action.status = RecoveryActionStatus.POLICY_REJECTED
    session.add(
        AuditLog(
            recovery_case_id=action.recovery_case_id,
            entity_type="recovery_action",
            entity_id=action.id,
            event_type="REVIEW_REJECTED",
            actor=AuditActor.HUMAN,
            description=f"{reviewer} rejected {action.action_type.value}. {note or ''}".strip(),
            occurred_at=_now(),
        )
    )
    await session.flush()
    return {
        "action_id": str(action.id),
        "case_id": str(action.recovery_case_id),
        "action_type": action.action_type.value,
        "decision": "REJECTED",
        "reviewer": reviewer,
    }


async def publish_resolution(result: dict) -> None:
    publisher = get_publisher()
    await publisher.publish(topics.REVIEW_RESOLVED, result["case_id"], result)
    if result["decision"] == "APPROVED":
        await publisher.publish(
            topics.RECOVERY_ACTION_COMPLETED,
            result["case_id"],
            {
                "case_id": result["case_id"],
                "action_id": result["action_id"],
                "action_type": result["action_type"],
                "outcome": "approved_by_human",
            },
        )
