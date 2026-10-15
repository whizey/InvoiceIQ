"""Opt-in workspace authorization for existing financial read APIs.

Once ANALYTICS_API_KEYS is configured, legacy reads also require a scope.
Unconfigured legacy local installations retain their previous single-workspace
API contract. New /dashboard/analytics always requires explicit authorization.
"""
import os
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import event, select, or_, and_
from sqlalchemy.orm import Session, with_loader_criteria
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from app.core.analytics_auth import require_analytics
from app.core.config import settings
from app.models import Company, Contact, Invoice, RecoveryCase, RecoveryAction, AgentDecision, AuditLog, CommunicationLog, PolicyDecision, PromiseToPay, Payment, PaymentEvent, InvoiceStateSnapshot, CaseStateSnapshot, InferenceRecord


class ScopedFinancialReadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        protected = path.startswith(("/invoices", "/companies", "/recovery-cases")) or path in ("/dashboard/metrics", "/dashboard/policy-overrides")
        inference = request.method == "POST" and path in {"/ml/payment-risk/predict", "/ml/process-survival/predict"}
        # Configured company scopes apply to externally triggered investigations
        # too. Reviewer tokens retain their existing global operator authority.
        review = path.startswith("/recovery-cases/actions/") and path.endswith(("/approve", "/reject"))
        scoped_write = request.method == "POST" and (protected or path.startswith("/voice")) and not review
        public = os.getenv("PUBLIC_READ_ONLY", "").lower() == "true"
        operations_read = (
            request.method == "GET"
            and path.startswith("/recovery-cases/")
            and path.endswith("/agent-operations")
        )
        authentication_required = (
            public
            or bool(os.getenv("ANALYTICS_API_KEYS", "").strip())
            or settings.env.lower() in {"production", "staging"}
        )
        if (
            (request.method == "GET" and protected)
            or inference
            or scoped_write
        ) and (authentication_required or operations_read):
            authorization = request.headers.get("authorization", "").split(" ", 1)
            supplied = request.headers.get("x-invoiceiq-analytics-key") or (authorization[1] if len(authorization) == 2 and authorization[0].lower() == "bearer" else "")
            try:
                credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=supplied) if supplied else None
                if public:
                    from app.core.db import async_session_factory
                    async with async_session_factory() as db:
                        identity = await require_analytics(credentials, db)
                else:
                    from app.core.db import async_session_factory
                    async with async_session_factory() as db:
                        identity = await require_analytics(credentials, db)
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)
            request.state.analytics_scope = identity.company_ids
            request.state.analytics_workspace = identity.workspace_id
        return await call_next(request)


@event.listens_for(Session, "do_orm_execute")
def scope_financial_select(execute_state):
    scope = execute_state.session.info.get("analytics_scope")
    if scope is None or not execute_state.is_select:
        return
    case_ids = select(RecoveryCase.id).where(RecoveryCase.company_id.in_(scope))
    invoice_ids = select(Invoice.id).where(Invoice.company_id.in_(scope))
    action_ids = select(RecoveryAction.id).where(RecoveryAction.recovery_case_id.in_(case_ids))
    criteria = [(Company, Company.id.in_(scope)), (Contact, Contact.company_id.in_(scope)),
        (Invoice, Invoice.company_id.in_(scope)), (RecoveryCase, RecoveryCase.company_id.in_(scope)),
        (RecoveryAction, RecoveryAction.recovery_case_id.in_(case_ids)),
        (AgentDecision, AgentDecision.recovery_case_id.in_(case_ids)),
        (AuditLog, or_(AuditLog.recovery_case_id.in_(case_ids), and_(
            AuditLog.recovery_case_id.is_(None), AuditLog.entity_type == "invoice",
            AuditLog.entity_id.in_(invoice_ids)))),
        (CommunicationLog, CommunicationLog.recovery_case_id.in_(case_ids)),
        (PromiseToPay, PromiseToPay.recovery_case_id.in_(case_ids)),
        (Payment, Payment.invoice_id.in_(invoice_ids)), (PaymentEvent, PaymentEvent.invoice_id.in_(invoice_ids)),
        (PolicyDecision, PolicyDecision.recovery_action_id.in_(action_ids)),
        (InvoiceStateSnapshot, InvoiceStateSnapshot.company_id.in_(scope)),
        (CaseStateSnapshot, CaseStateSnapshot.company_id.in_(scope)),
        (InferenceRecord, InferenceRecord.company_id.in_(scope))]
    execute_state.statement = execute_state.statement.options(*[
        with_loader_criteria(model, criterion, include_aliases=True) for model, criterion in criteria])
