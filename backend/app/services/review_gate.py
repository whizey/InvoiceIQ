"""Human approval gate for actions the policy engine holds.

The policy engine returns REQUIRES_HUMAN_REVIEW for high-value reminders.
graph.policy_check_node stores those as PENDING_APPROVAL and the workflow
stops. Nothing is sent until approve() runs; reject() records the refusal.
Both write an AuditLog row with actor HUMAN.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import get_publisher, topics
from app.events.outbox import TransactionalEventPublisher
from app.models import AuditLog, Invoice, RecoveryAction, RecoveryCase
from app.models.enums import AuditActor, RecoveryActionStatus, RecoveryCaseStatus, InvoiceStatus, PolicyDecisionResult
from app.services.action_policy import primary_contact, evaluate_policy_for_case
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
            select(RecoveryAction).where(RecoveryAction.id == action_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if action is None:
        raise ReviewError("Action not found", 404)
    if action.status != RecoveryActionStatus.PENDING_APPROVAL:
        raise ReviewError(f"Action is {action.status.value}, not waiting for approval")
    return action


async def list_pending(session: AsyncSession, *, start=None, stop=None, company_ids=None, posting_start=None, posting_end=None) -> list[dict]:
    stmt = select(RecoveryAction).where(RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL)
    if start is not None:
        stmt = stmt.where(RecoveryAction.created_at >= start, RecoveryAction.created_at < stop)
    if company_ids is not None:
        stmt = stmt.where(RecoveryAction.recovery_case_id.in_(select(RecoveryCase.id).where(RecoveryCase.company_id.in_(company_ids))))
    if posting_start is not None:
        invoices = select(Invoice.id).where(func.coalesce(Invoice.posting_date, Invoice.issue_date).between(posting_start, posting_end))
        stmt = stmt.where(RecoveryAction.recovery_case_id.in_(select(RecoveryCase.id).where(RecoveryCase.invoice_id.in_(invoices))))
    rows = (await session.execute(stmt.order_by(RecoveryAction.created_at, RecoveryAction.id))).scalars().all()
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
    case = (await session.execute(
        select(RecoveryCase).where(RecoveryCase.id == action.recovery_case_id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if case is None or case.status in {
        RecoveryCaseStatus.RECOVERED, RecoveryCaseStatus.CLOSED, RecoveryCaseStatus.CLOSED_UNRECOVERED,
    }:
        raise ReviewError("Case is no longer eligible for recovery")
    invoice = (await session.execute(
        select(Invoice).where(Invoice.id == case.invoice_id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if invoice is None or invoice.status in {InvoiceStatus.PAID, InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF}:
        raise ReviewError("Invoice is no longer eligible for recovery")
    if getattr(invoice, "company_id", case.company_id) != case.company_id:
        raise ReviewError("Invoice does not belong to the recovery case company")
    if invoice.amount_paid >= invoice.amount_total:
        raise ReviewError("Invoice has no outstanding balance")
    current = await evaluate_policy_for_case(session, case, invoice, action.action_type)
    if current.final_action != action.action_type or current.decision == PolicyDecisionResult.REJECTED:
        raise ReviewError("Policy changed since recommendation; run a new investigation")
    contact = await primary_contact(session, case.company_id)

    from app.services.action_execution import execute_authorized_action
    from app.agents.graph import record_outcome_node
    execution = await execute_authorized_action(
        session, action, case, invoice, contact, executor=execute_mock_action, human_authorized=True,
    )
    outcome = await record_outcome_node({
        "case_id": str(case.id), "final_action": action.action_type.value,
        "action_result": execution,
    }, session)
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
    result = {
        "action_id": str(action.id),
        "case_id": str(case.id),
        "action_type": action.action_type.value,
        "decision": "APPROVED",
        "execution_status": action.status.value,
        "outcome": outcome["outcome_summary"],
        "simulated": execution.get("simulated", False),
        "reviewer": reviewer,
    }
    await publish_resolution(result, publisher=TransactionalEventPublisher(session))
    return result


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
    result = {
        "action_id": str(action.id),
        "case_id": str(action.recovery_case_id),
        "action_type": action.action_type.value,
        "decision": "REJECTED",
        "reviewer": reviewer,
    }
    await publish_resolution(result, publisher=TransactionalEventPublisher(session))
    return result


async def publish_resolution(result: dict, *, publisher=None) -> None:
    publisher = publisher or get_publisher()
    await publisher.publish(topics.REVIEW_RESOLVED, result["case_id"], result)
    if result["decision"] == "APPROVED" and result.get("execution_status", "EXECUTED") == "EXECUTED":
        await publisher.publish(
            topics.RECOVERY_ACTION_COMPLETED,
            result["case_id"],
            {
                "case_id": result["case_id"],
                "action_id": result["action_id"],
                "action_type": result["action_type"],
                "outcome": result.get("outcome", "approved_by_human"),
                "simulated": result.get("simulated", False),
            },
        )
