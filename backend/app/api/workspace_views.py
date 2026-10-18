from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.db import get_db
from app.core.analytics_auth import AnalyticsIdentity, require_analytics
from app.core.reviewer_auth import ReviewerIdentity, require_reviewer
from app.core.reviewer_company_scope import configured_reviewer_scopes
from app.core.config import settings
from app.models import RecoveryCase, RecoveryAction, Invoice, Company
from app.models.enums import RecoveryActionStatus
from app.schemas.recovery import RecoveryCaseDetailOut
from app.api.recovery_cases import _load_case_detail

router = APIRouter(prefix="/dashboard/workspace", tags=["workspace"])


async def analytics_header(
    key: str | None = Header(None, alias="X-InvoiceIQ-Analytics-Key"), db: AsyncSession = Depends(get_db)
):
    if not key:
        raise HTTPException(401, "Analytics token required")
    return await require_analytics(HTTPAuthorizationCredentials(scheme="Bearer", credentials=key), db)


@router.get("/cases")
async def cases(identity: AnalyticsIdentity = Depends(require_analytics), db: AsyncSession = Depends(get_db)):
    if not identity.company_ids:
        return {"cases": []}

    rows = (
        (
            await db.execute(
                select(RecoveryCase)
                .where(RecoveryCase.company_id.in_(identity.company_ids))
                .options(
                    selectinload(RecoveryCase.invoice).selectinload(Invoice.company),
                    selectinload(RecoveryCase.actions),
                )
                .order_by(RecoveryCase.opened_at.desc())
            )
        )
        .scalars()
        .all()
    )

    return {
        "cases": [
            {
                "id": str(c.id),
                "company": c.invoice.company.name,
                "invoice": c.invoice.invoice_number,
                "status": c.status.value,
                "opened": (c.opened_at.isoformat() if c.opened_at else None),
                "outstanding": str(max(c.invoice.amount_total - c.invoice.amount_paid, 0)),
                "closed_at": c.closed_at.isoformat() if c.closed_at else None,
                "recovered": str(c.recovered_amount),
                "pending": any(a.status == RecoveryActionStatus.PENDING_APPROVAL for a in c.actions),
            }
            for c in rows
        ]
    }


@router.get("/cases/{case_id}", response_model=RecoveryCaseDetailOut)
async def detail(
    case_id: UUID,
    identity: AnalyticsIdentity = Depends(require_analytics),
    db: AsyncSession = Depends(get_db),
):
    allowed = await db.scalar(
        select(RecoveryCase.id).where(
            RecoveryCase.id == case_id, RecoveryCase.company_id.in_(identity.company_ids)
        )
    )

    if not allowed:
        raise HTTPException(404, "Case not found")

    return await _load_case_detail(db, case_id)


@router.get("/reviews")
async def reviews(
    reviewer: ReviewerIdentity = Depends(require_reviewer),
    identity: AnalyticsIdentity = Depends(analytics_header),
    db: AsyncSession = Depends(get_db),
):
    scope = configured_reviewer_scopes()

    if scope is None and settings.env.lower() in {"production", "prod", "staging"}:
        raise HTTPException(503, "Reviewer company scope is not configured")

    allowed = (
        set(identity.company_ids)
        if scope is None
        else set(identity.company_ids).intersection(scope.get(reviewer.reviewer_id, frozenset()))
    )

    if not allowed:
        return {"reviews": []}

    rows = (
        await db.execute(
            select(RecoveryAction, RecoveryCase, Invoice, Company)
            .join(RecoveryCase, RecoveryAction.recovery_case_id == RecoveryCase.id)
            .join(Invoice, RecoveryCase.invoice_id == Invoice.id)
            .join(Company, RecoveryCase.company_id == Company.id)
            .where(
                RecoveryCase.company_id.in_(allowed),
                RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL,
            )
            .order_by(RecoveryAction.created_at.asc())
        )
    ).all()

    return {
        "reviews": [
            {
                "id": str(a.id),
                "case_id": str(c.id),
                "company": co.name,
                "invoice": i.invoice_number,
                "action": a.action_type.value,
                "created": (a.created_at.isoformat() if a.created_at else None),
                "outstanding": str(max(i.amount_total - i.amount_paid, 0)),
            }
            for a, c, i, co in rows
        ]
    }


# Authorized review endpoints; no changes to existing workspace reads.

from typing import Literal as _ReviewLiteral  # noqa: E402
from uuid import UUID as _ReviewUUID  # noqa: E402
from fastapi import Depends as _ReviewDepends, Header as _ReviewHeader, HTTPException as _ReviewHTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials as _ReviewCredentials  # noqa: E402
from sqlalchemy import select as _review_select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession as _ReviewSession  # noqa: E402
from app.core.analytics_auth import (  # noqa: E402
    require_analytics as _require_analytics,
    AnalyticsIdentity as _ReviewAnalytics,
)
from app.core.db import get_db as _review_db  # noqa: E402
from app.core.reviewer_auth import (  # noqa: E402
    ReviewerIdentity as _ReviewerIdentity,
    require_reviewer as _require_reviewer,
)
from app.core.reviewer_company_scope import (  # noqa: E402
    configured_reviewer_scopes as _review_scopes,
    require_scoped_reviewer as _scoped_reviewer,
)
from app.core.config import settings as _review_settings  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog as _AuditLog,
    RecoveryCase as _RecoveryCase,
    RecoveryAction as _RecoveryAction,
    Company as _Company,
)
from app.services import review_gate as _review_gate  # noqa: E402
from pydantic import BaseModel as _ReviewBase, Field as _ReviewField  # noqa: E402


class _WorkspaceDecision(_ReviewBase):
    note: str = _ReviewField(min_length=1, max_length=2000)


async def _workspace_analytics(
    key: str | None = _ReviewHeader(None, alias="X-InvoiceIQ-Analytics-Key"),
    db: _ReviewSession = _ReviewDepends(_review_db),
) -> _ReviewAnalytics:
    if not key:
        raise _ReviewHTTPException(401, "Analytics workspace credentials required")
    return await _require_analytics(_ReviewCredentials(scheme="Bearer", credentials=key), db)


def _workspace_scope(identity: _ReviewAnalytics, reviewer: _ReviewerIdentity):
    scopes = _review_scopes()
    if scopes is None:
        if _review_settings.env.lower() in {"production", "staging", "prod"}:
            raise _ReviewHTTPException(503, "Reviewer company authorization is not configured")
        return identity.company_ids
    allowed = scopes.get(reviewer.reviewer_id, frozenset())
    if not allowed:
        raise _ReviewHTTPException(403, "Reviewer has no permitted companies")
    return tuple(cid for cid in identity.company_ids if cid in allowed)


@router.get("/review-history")
async def workspace_review_history(
    reviewer: _ReviewerIdentity = _ReviewDepends(_require_reviewer),
    identity: _ReviewAnalytics = _ReviewDepends(_workspace_analytics),
    db: _ReviewSession = _ReviewDepends(_review_db),
):
    companies = _workspace_scope(identity, reviewer)
    if not companies:
        return {"history": []}
    query = (
        _review_select(_AuditLog, _RecoveryCase, _Company, _RecoveryAction)
        .join(_RecoveryCase, _AuditLog.recovery_case_id == _RecoveryCase.id)
        .join(_Company, _RecoveryCase.company_id == _Company.id)
        .outerjoin(_RecoveryAction, _AuditLog.entity_id == _RecoveryAction.id)
        .where(
            _RecoveryCase.company_id.in_(companies),
            _AuditLog.event_type.in_(("REVIEW_APPROVED", "REVIEW_REJECTED")),
        )
        .order_by(_AuditLog.occurred_at.desc(), _AuditLog.id)
        .limit(500)
    )
    rows = (await db.execute(query)).all()
    return {
        "history": [
            {
                "id": str(a.id),
                "action_id": str(x.id) if x else None,
                "case_id": str(c.id),
                "company_name": co.name,
                "action_type": x.action_type.value if x else "Unknown action",
                "decision": "Approved" if a.event_type == "REVIEW_APPROVED" else "Rejected",
                "description": a.description,
                "decided_at": a.occurred_at.isoformat(),
                "review_hours": round((a.occurred_at - x.created_at).total_seconds() / 3600, 2)
                if x and x.created_at and a.occurred_at
                else None,
            }
            for a, c, co, x in rows
        ]
    }


@router.post("/reviews/{action_id}/{decision}")
async def workspace_review_decision(
    action_id: _ReviewUUID,
    decision: _ReviewLiteral["approve", "reject"],
    body: _WorkspaceDecision,
    reviewer: _ReviewerIdentity = _ReviewDepends(_require_reviewer),
    identity: _ReviewAnalytics = _ReviewDepends(_workspace_analytics),
    db: _ReviewSession = _ReviewDepends(_review_db),
):
    permitted = _workspace_scope(identity, reviewer)
    company = await db.scalar(
        _review_select(_RecoveryCase.company_id)
        .join(_RecoveryAction, _RecoveryAction.recovery_case_id == _RecoveryCase.id)
        .where(_RecoveryAction.id == action_id)
    )
    if company is None or company not in permitted:
        raise _ReviewHTTPException(404, "Review action not found")
    await _scoped_reviewer(action_id=action_id, db=db, reviewer=reviewer)
    try:
        outcome = (
            await _review_gate.approve(db, action_id, reviewer.reviewer_id, body.note)
            if decision == "approve"
            else await _review_gate.reject(db, action_id, reviewer.reviewer_id, body.note)
        )
    except _review_gate.ReviewError as e:
        raise _ReviewHTTPException(e.status_code, str(e)) from None
    await db.commit()
    return outcome


# INVOICEIQ_POLICY_REVIEW_REQUESTS_V3
from uuid import UUID as _ReviewUUID  # noqa: E402
import hmac as _review_hmac  # noqa: E402
import os as _review_os  # noqa: E402
from fastapi import Header as _ReviewHeader  # noqa: E402
from pydantic import BaseModel as _ReviewModel, Field as _ReviewField  # noqa: E402
from sqlalchemy.orm import selectinload as _review_selectinload  # noqa: E402
from app.core.time import utc_today as _review_today  # noqa: E402
from app.models import (  # noqa: E402
    RecoveryCase as _RCase,
    RecoveryAction as _RAction,
    Invoice as _RInvoice,
    AuditLog as _RAudit,
)
from app.models.enums import (  # noqa: E402
    AuditActor as _RActor,
    InvoiceStatus as _IStatus,
    PolicyDecisionResult as _PResult,
    ProposedBy as _PBy,
    RecoveryActionStatus as _AStatus,
    RecoveryActionType as _AType,
    RecoveryCaseStatus as _CStatus,
)
from app.core.reviewer_auth import _configured_reviewers as _review_keys  # noqa: E402
from app.services.action_policy import (  # noqa: E402
    evaluate_policy_for_case as _review_evaluate,
    evaluate_and_record_action as _review_record,
)
from app.services.policy_engine import HIGH_VALUE_THRESHOLD as _REVIEW_THRESHOLD  # noqa: E402


class _ReviewRequestBody(_ReviewModel):
    case_id: _ReviewUUID
    note: str = _ReviewField(min_length=3, max_length=1000)


async def _review_assess(db, case, invoice):
    balance = invoice.amount_total - invoice.amount_paid

    result = dict(
        case_id=str(case.id),
        company_name=invoice.company.name if invoice.company else "Unknown",
        invoice_number=invoice.invoice_number,
        outstanding_amount=str(max(balance, 0)),
        status=case.status.value,
        eligible=False,
        reason="",
    )

    if case.status in {
        _CStatus.RECOVERED,
        _CStatus.CLOSED,
        _CStatus.CLOSED_UNRECOVERED,
    }:
        result["reason"] = "Case already closed or recovered."

    elif (
        invoice.status
        in {
            _IStatus.PAID,
            _IStatus.CANCELLED,
            _IStatus.WRITTEN_OFF,
        }
        or balance <= 0
    ):
        result["reason"] = "Invoice has no collectible outstanding balance."

    elif invoice.company_id != case.company_id:
        result["reason"] = "Invoice/company mismatch."

    elif invoice.due_date >= _review_today():
        result["reason"] = "Invoice is not overdue."

    elif (
        await db.scalar(
            _review_select(_RAction.id)
            .where(
                _RAction.recovery_case_id == case.id,
                _RAction.status == _AStatus.PENDING_APPROVAL,
            )
            .limit(1)
        )
        is not None
    ):
        result["reason"] = "A pending approval already exists."

    else:
        policy = await _review_evaluate(db, case, invoice, _AType.SEND_EMAIL)

        result["reason"] = policy.reason

        result["eligible"] = (
            policy.decision == _PResult.REQUIRES_HUMAN_REVIEW and policy.final_action == _AType.SEND_EMAIL
        )

    return result


@router.get("/review-candidates")
async def _review_candidates(
    identity=Depends(require_analytics),
    db=Depends(get_db),
):
    scopes = _review_scopes() or {}

    permitted = set().union(*scopes.values()) if scopes else set()
    allowed = set(identity.company_ids) & permitted

    if not allowed:
        return {"candidates": []}

    cases = (
        (
            await db.execute(
                _review_select(_RCase)
                .where(_RCase.company_id.in_(allowed))
                .options(_review_selectinload(_RCase.invoice).selectinload(_RInvoice.company))
                .order_by(_RCase.id)
            )
        )
        .scalars()
        .all()
    )

    results = []

    for case in cases:
        if case.invoice and float(case.revenue_at_risk) >= _REVIEW_THRESHOLD:
            results.append(await _review_assess(db, case, case.invoice))

    return {"candidates": results}


@router.post("/review-requests", status_code=201)
async def _create_review_request(
    payload: _ReviewRequestBody,
    identity=Depends(require_analytics),
    db=Depends(get_db),
    reviewer_key: str | None = _ReviewHeader(None, alias="X-InvoiceIQ-Reviewer-Key"),
):
    if _review_os.getenv("PUBLIC_READ_ONLY", "").lower() == "true":
        raise HTTPException(403, "Public read-only mode")

    if not reviewer_key:
        raise HTTPException(401, "Reviewer key required")

    reviewer = next(
        (name for name, key in _review_keys().items() if _review_hmac.compare_digest(key, reviewer_key)),
        None,
    )

    if reviewer is None:
        raise HTTPException(403, "Invalid reviewer key")

    allowed = set(identity.company_ids) & set((_review_scopes() or {}).get(reviewer, ()))

    if not allowed:
        raise HTTPException(403, "No authorized companies")

    case = (
        await db.execute(
            _review_select(_RCase)
            .where(
                _RCase.id == payload.case_id,
                _RCase.company_id.in_(allowed),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if case is None:
        raise HTTPException(404, "Case not found")

    invoice = (
        await db.execute(
            _review_select(_RInvoice)
            .where(_RInvoice.id == case.invoice_id)
            .with_for_update()
            .options(_review_selectinload(_RInvoice.company))
        )
    ).scalar_one_or_none()

    if invoice is None:
        raise HTTPException(404, "Invoice not found")

    current = await _review_assess(db, case, invoice)

    if not current["eligible"]:
        raise HTTPException(409, current["reason"])

    held = await _review_record(
        db,
        case,
        invoice,
        _AType.SEND_EMAIL,
        proposed_by=_PBy.HUMAN,
    )

    if (
        held.outcome.decision != _PResult.REQUIRES_HUMAN_REVIEW
        or held.action.status != _AStatus.PENDING_APPROVAL
        or held.outcome.final_action != _AType.SEND_EMAIL
    ):
        await db.rollback()
        raise HTTPException(409, "Policy changed; no request created")

    db.add(
        _RAudit(
            recovery_case_id=case.id,
            entity_type="recovery_action",
            entity_id=held.action.id,
            event_type="REVIEW_REQUESTED",
            actor=_RActor.HUMAN,
            description=("Reviewer requested policy hold: " + payload.note.strip()),
        )
    )

    await db.commit()

    return {
        "action_id": str(held.action.id),
        "status": "PENDING_APPROVAL",
        "message": "Approval requested. No email sent.",
    }
