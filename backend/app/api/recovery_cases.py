from typing import Literal
from typing import Annotated
from datetime import date, datetime, timezone
from app.core.time import utc_today
from uuid import UUID, uuid5, NAMESPACE_URL

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.graph import run_recovery_cycle
from app.core.config import settings
from app.core.db import get_db
from app.core.case_company_scope import require_case_company_scope
from app.core.locks import LockAcquisitionError, acquire_lock
from app.core.reviewer_auth import ReviewerIdentity, require_reviewer
from app.core.reviewer_company_scope import require_scoped_reviewer
from app.models import AuditLog, Company, Invoice, RecoveryAction, RecoveryCase
from app.models.enums import AuditActor, ProposedBy, RecoveryActionStatus, RecoveryActionType
from app.schemas.recovery import (
    AuditLogOut,
    DetectionSummaryOut,
    RecoveryCaseDetailOut,
    RecoveryCaseListItemOut,
    RiskExplanationOut,
    SendReminderEmailOut,
)
from app.services import review_gate
from app.services.action_policy import evaluate_and_record_action, primary_contact
from app.services.risk_engine import run_detection
from app.tools.email_provider import send_reminder_email
from app.tools.mock_tools import execute_mock_action

router = APIRouter(prefix="/recovery-cases", tags=["recovery-cases"])


async def _load_case_detail(db: AsyncSession, case_id: UUID) -> RecoveryCase | None:
    stmt = (
        select(RecoveryCase)
        .where(RecoveryCase.id == case_id)
        .options(
            selectinload(RecoveryCase.invoice).selectinload(Invoice.company),
            selectinload(RecoveryCase.actions).selectinload(RecoveryAction.policy_decisions),
            selectinload(RecoveryCase.agent_decisions),
            selectinload(RecoveryCase.promises_to_pay),
            selectinload(RecoveryCase.communication_logs),
            selectinload(RecoveryCase.audit_logs),
        )
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


@router.post("/detect-overdue", response_model=DetectionSummaryOut)
async def detect_overdue(db: AsyncSession = Depends(get_db)) -> DetectionSummaryOut:
    """Deterministic engine trigger: mark newly-overdue invoices, open cases for
    them, and resolve any promise-to-pay commitments whose date has passed.

    Also run automatically by the in-process scheduler when SCHEDULER_ENABLED
    is set. Returns 409 if a sweep is already in progress.
    """
    try:
        async with acquire_lock("detect-overdue"):
            result = await run_detection(db)
    except LockAcquisitionError:
        raise HTTPException(status_code=409, detail="A detection sweep is already in progress") from None
    return DetectionSummaryOut(
        invoices_marked_overdue=len(result.invoices_marked_overdue),
        cases_created=len(result.cases_created),
        case_ids=[c.id for c in result.cases_created],
        promises_fulfilled=len(result.promises_checked.fulfilled),
        promises_broken=len(result.promises_checked.broken),
    )


def _to_list_item(case: RecoveryCase) -> RecoveryCaseListItemOut:
    days_overdue = (utc_today() - case.invoice.due_date).days
    current_action = (
        case.actions[-1].action_type
        if case.actions and case.actions[-1].status != RecoveryActionStatus.PROPOSED
        else None
    )
    return RecoveryCaseListItemOut(
        id=case.id,
        invoice_id=case.invoice.id,
        company_name=case.invoice.company.name,
        invoice_number=case.invoice.invoice_number,
        amount_total=case.invoice.amount_total,
        days_overdue=days_overdue,
        status=case.status,
        risk_level=case.risk_level,
        recovery_probability=case.recovery_probability,
        current_action=current_action,
        recovered_amount=case.recovered_amount,
        outstanding_amount=max(case.invoice.amount_total - case.invoice.amount_paid, 0),
        pending_approval=any(action.status == RecoveryActionStatus.PENDING_APPROVAL for action in case.actions),
    )


@router.get("", response_model=list[RecoveryCaseListItemOut])
async def list_recovery_cases(db: AsyncSession = Depends(get_db)) -> list[RecoveryCaseListItemOut]:
    stmt = (
        select(RecoveryCase)
        .options(
            selectinload(RecoveryCase.invoice).selectinload(Invoice.company),
            selectinload(RecoveryCase.actions),
        )
        .order_by(RecoveryCase.opened_at.desc())
    )
    result = await db.execute(stmt)
    cases = result.scalars().all()
    return [_to_list_item(c) for c in cases]


@router.get(
    "/{case_id}/risk-explanation",
    response_model=RiskExplanationOut,
)
async def get_risk_explanation(
    case_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> RiskExplanationOut:
    """Read stored B2B inference, never a synthetic recovery model."""
    case = await _load_case_detail(db, case_id)
    if case is None:
        raise HTTPException(
            status_code=404,
            detail="Recovery case not found",
        )

    invoice = case.invoice
    return RiskExplanationOut(
        prediction_target="late_payment_weekend_adjusted",
        experimental=True,
        model_name=invoice.payment_risk_model,
        model_version=invoice.payment_risk_model_version,
        scored_at=invoice.payment_risk_scored_at,
        late_payment_probability=(
            float(invoice.late_payment_probability)
            if invoice.late_payment_probability is not None
            else None
        ),
        risk_score=(
            float(case.risk_score)
            if case.risk_score is not None else None
        ),
        risk_level=case.risk_level,
        contributions=invoice.payment_risk_drivers or [],
    )


class ReviewDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None


@router.get("/pending-reviews")
async def list_pending_reviews(
    start_date: date | None = None,
    end_date: date | None = None,
    workflow_date_axis: Literal["event", "invoice"] = "event",
    analytics_key: str | None = Header(None, alias="X-InvoiceIQ-Analytics-Key"),
    db: AsyncSession = Depends(get_db),
    _reviewer: ReviewerIdentity = Depends(
        require_reviewer
    ),
) -> list[dict]:
    """Actions held by the policy engine, waiting for a person to approve or reject."""
    if start_date is None and end_date is None:
        return await review_gate.list_pending(db)
    if start_date is None or end_date is None:
        raise HTTPException(422, "Both start_date and end_date are required")
    from app.schemas.analytics import AnalyticsRange
    from pydantic import ValidationError
    try:
        start, stop = AnalyticsRange(start_date=start_date, end_date=end_date).timestamps(settings.business_timezone)
    except ValidationError:
        raise HTTPException(422, "Invalid review date interval") from None
    from app.core.analytics_auth import require_analytics
    from fastapi.security import HTTPAuthorizationCredentials
    scope = await require_analytics(HTTPAuthorizationCredentials(scheme="Bearer", credentials=analytics_key) if analytics_key else None, db)
    if workflow_date_axis == "invoice":
        return await review_gate.list_pending(db, company_ids=scope.company_ids, posting_start=start_date, posting_end=end_date)
    return await review_gate.list_pending(db, start=start, stop=stop, company_ids=scope.company_ids)


@router.post("/actions/{action_id}/approve")
async def approve_action(
    action_id: UUID,
    body: ReviewDecisionIn,
    db: AsyncSession = Depends(get_db),
    reviewer: ReviewerIdentity = Depends(
        require_scoped_reviewer
    ),
) -> dict:
    try:
        result = await review_gate.approve(
            db,
            action_id,
            reviewer.reviewer_id,
            body.note,
        )
    except review_gate.ReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    await db.commit()
    return result


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: UUID,
    body: ReviewDecisionIn,
    db: AsyncSession = Depends(get_db),
    reviewer: ReviewerIdentity = Depends(
        require_scoped_reviewer
    ),
) -> dict:
    try:
        result = await review_gate.reject(
            db,
            action_id,
            reviewer.reviewer_id,
            body.note,
        )
    except review_gate.ReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    await db.commit()
    return result


@router.get("/{case_id}", response_model=RecoveryCaseDetailOut)
async def get_recovery_case(case_id: UUID, db: AsyncSession = Depends(get_db)) -> RecoveryCase:
    case = await _load_case_detail(db, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")
    return case


@router.post("/{case_id}/run", response_model=RecoveryCaseDetailOut, dependencies=[Depends(require_case_company_scope)])
async def run_recovery_case(case_id: UUID, db: AsyncSession = Depends(get_db), idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=100)] = None) -> RecoveryCase:
    """Advance this case by exactly one recovery cycle through the LangGraph workflow.

    A no-op (case unchanged) if the case is already in a terminal state.
    Returns 409 if a cycle is already running for this case.
    """
    existing = await db.get(RecoveryCase, case_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    try:
        async with acquire_lock(f"recovery-case:{case_id}"):
            # DB row lock also protects replicas when the optional Redis layer is absent.
            await db.execute(select(RecoveryCase).where(RecoveryCase.id == case_id).with_for_update().execution_options(populate_existing=True))
            receipt_id = uuid5(NAMESPACE_URL, f"invoiceiq:run:{case_id}:{idempotency_key}") if idempotency_key else None
            if receipt_id and await db.get(AuditLog, receipt_id):
                await db.rollback()
                return await _load_case_detail(db, case_id)
            if receipt_id:
                db.add(AuditLog(id=receipt_id, recovery_case_id=case_id, entity_type="recovery_case",
                    entity_id=case_id, event_type="RECOVERY_RUN_REQUEST", actor=AuditActor.SYSTEM,
                    description="Idempotent recovery investigation request.", metadata_={"idempotency_key": idempotency_key}))
            await run_recovery_cycle(db, case_id)
    except LockAcquisitionError:
        raise HTTPException(
            status_code=409, detail="A recovery cycle is already running for this case"
        ) from None

    case = await _load_case_detail(db, case_id)
    return case


@router.post("/{case_id}/send-reminder-email", response_model=SendReminderEmailOut, dependencies=[Depends(require_case_company_scope)])
async def send_reminder_email_endpoint(
    case_id: UUID, db: AsyncSession = Depends(get_db)
) -> SendReminderEmailOut:
    """Human-triggered real reminder email — always to settings.sample_notify_email,
    never a seeded contact's @example.com. Still goes through evaluate_policy()
    with the same reminder cap/cooldown rules as an automated cycle (proposed_by
    is HUMAN here, not AI, but the gate is identical).

    Errors clearly (400) if SAMPLE_NOTIFY_EMAIL isn't configured, rather than
    silently falling back to any other address. Returns 409 if a cycle/other
    action is already running for this case.
    """
    if not settings.sample_notify_email:
        raise HTTPException(
            status_code=400,
            detail=(
                "SAMPLE_NOTIFY_EMAIL is not configured. Set it in backend/.env and "
                "restart the API before sending a reminder."
            ),
        )

    case = await db.get(RecoveryCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    try:
        async with acquire_lock(f"recovery-case:{case_id}"):
            invoice = await db.get(Invoice, case.invoice_id)
            company = await db.get(Company, case.company_id)
            contact = await primary_contact(db, case.company_id)

            recorded = await evaluate_and_record_action(
                db, case, invoice, RecoveryActionType.SEND_EMAIL, proposed_by=ProposedBy.HUMAN
            )
            action = recorded.action
            outcome = recorded.outcome
            now = datetime.now(timezone.utc)

            if action.status == RecoveryActionStatus.PENDING_APPROVAL:
                # Do not contact anyone until review_gate.approve() resolves it.
                to = None
                status = "PENDING_APPROVAL"
                sent_at = None
                description = "Reminder queued for human review; no message sent."
            elif outcome.final_action == RecoveryActionType.SEND_EMAIL:
                result = await send_reminder_email(db, case, invoice, contact, company.name)
                action.status = RecoveryActionStatus.EXECUTED
                action.executed_at = now
                action.result = result
                to = result["to"]
                status = result["status"]  # "SENT" or "SIMULATED"
                sent_at: datetime | None = now
                description = (
                    f"Real reminder email {'sent' if status == 'SENT' else 'simulated (no email provider configured)'} "
                    f"to {to}."
                )
            else:
                result = await execute_mock_action(db, outcome.final_action, case, invoice, contact)
                action.status = RecoveryActionStatus.EXECUTED
                action.executed_at = now
                action.result = result
                to = None
                status = "REJECTED"
                sent_at = None
                description = f"Policy substituted {outcome.final_action.value} instead of SEND_EMAIL: {outcome.reason}"

            await db.flush()
            db.add(
                AuditLog(
                    recovery_case_id=case.id,
                    entity_type="recovery_action",
                    entity_id=action.id,
                    event_type=(
                        "REVIEW_REQUESTED" if status == "PENDING_APPROVAL"
                        else "EMAIL_SENT" if outcome.final_action == RecoveryActionType.SEND_EMAIL
                        else "POLICY_SUBSTITUTED"
                    ),
                    actor=AuditActor.SYSTEM,
                    description=description,
                    occurred_at=now,
                )
            )
            await db.commit()
    except LockAcquisitionError:
        raise HTTPException(
            status_code=409, detail="A recovery cycle is already running for this case"
        ) from None

    return SendReminderEmailOut(
        status=status,
        to=to,
        sent_at=sent_at,
        policy_decision=outcome.decision,
        reason=outcome.reason,
    )


@router.get("/{case_id}/audit-trail", response_model=list[AuditLogOut])
async def get_audit_trail(case_id: UUID, db: AsyncSession = Depends(get_db)) -> list[AuditLog]:
    case_exists = await db.execute(select(RecoveryCase.id).where(RecoveryCase.id == case_id))
    if case_exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    stmt = (
        select(AuditLog)
        .where(AuditLog.recovery_case_id == case_id)
        .order_by(AuditLog.occurred_at)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


# INVOICEIQ_PHASE7C_OPERATIONS_V2
from fastapi import Request as _OperationsRequest  # noqa: E402


@router.get("/{case_id}/agent-operations")
async def get_case_agent_operations(
    case_id: UUID,
    request: _OperationsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read-only company-scoped agent observability."""

    from math import ceil
    from sqlalchemy import text as sql_text
    from app.models import AgentDecision
    from app.services.agent_tracing import list_case_spans

    # Explicit authenticated scope is required.
    # The mutation guard is not suitable for read endpoints.
    authorized = getattr(
        request.state,
        "analytics_scope",
        None,
    )

    if not authorized:
        raise HTTPException(
            401,
            "Company-scoped authentication required",
        )

    allowed = {
        str(company_id)
        for company_id in authorized
    }

    db_scope = db.info.get("analytics_scope")

    if db_scope is not None:
        allowed.intersection_update(
            str(company_id)
            for company_id in db_scope
        )

    if not allowed:
        raise HTTPException(
            403,
            "No authorized companies",
        )

    case = await db.get(RecoveryCase, case_id)

    if (
        case is None
        or str(case.company_id) not in allowed
    ):
        raise HTTPException(
            404,
            "Recovery case not found",
        )

    # Graceful handling when production has not
    # received the opt-in tracing migration.
    migrated = await db.scalar(
        sql_text(
            "SELECT to_regclass("
            "'public.agent_node_spans')"
        )
    )

    spans = (
        await list_case_spans(
            db,
            case_id,
            authorized_company_ids=(case.company_id,),
            limit=100,
        )
        if migrated is not None
        else []
    )

    decisions = (
        await db.execute(
            select(
                AgentDecision.stage,
                AgentDecision.model_name,
            )
            .where(
                AgentDecision.recovery_case_id == case_id
            )
            .limit(40)
        )
    ).all()

    models = [
        {
            "stage": getattr(
                stage,
                "value",
                str(stage),
            ),
            "model": model,
        }
        for stage, model in decisions
    ]

    durations = sorted(
        float(span["duration_ms"])
        for span in spans
    )

    p95 = (
        durations[
            ceil(0.95 * len(durations)) - 1
        ]
        if durations
        else None
    )

    holds = sum(
        span["policy_decision"]
        == "REQUIRES_HUMAN_REVIEW"
        for span in spans
        if span["node"] == "policy_check"
    )

    return {
        "case_id": str(case_id),
        "status": (
            "schema_not_migrated"
            if migrated is None
            else "recorded"
            if spans
            else "no_recorded_traces"
        ),
        "metrics": {
            "node_executions": len(spans),
            "investigations": len({
                span["investigation_id"]
                for span in spans
            }),
            "average_latency_ms": (
                round(
                    sum(durations) / len(durations),
                    2,
                )
                if durations
                else None
            ),
            "p95_latency_ms": (
                round(p95, 2)
                if p95 is not None
                else None
            ),
            "human_review_holds": holds,
        },
        "spans": spans,
        "models": models,
    }
