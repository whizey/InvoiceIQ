"""Constrained NL2SQL: natural language -> typed plan -> allowlisted SELECT.

Internal tool only. Scope comes from a server-resolved case, never from an LLM.
Company scope is not tenant authentication; there is no public query endpoint.
Arbitrary SQL, joins, functions, expressions and identifiers are not accepted.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from app.agents.guardrails import is_untrusted_instruction
from app.models import Invoice, Payment
from app.models.enums import InvoiceStatus, PaymentStatus

SCHEMA = {
    "invoices": ("company_id", "invoice_number", "amount_total", "amount_paid", "due_date", "status", "currency"),
}


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: Literal["outstanding_invoices", "invoice_summary"]
    limit: int = Field(default=20, ge=1, le=50)


def plan_question(question: str) -> QueryPlan:
    if not question.strip() or len(question) > 500 or is_untrusted_instruction(question):
        raise ValueError("Question is outside the supported read-only contract")
    normalized = question.lower().strip().rstrip("?")
    # Explicit, narrow deterministic fallback. Unsupported questions fail closed.
    if normalized in {"show outstanding invoices", "list outstanding invoices", "what invoices remain unpaid"}:
        return QueryPlan(intent="outstanding_invoices")
    if normalized in {"summarize invoice history", "invoice summary", "how many invoices does this customer have"}:
        return QueryPlan(intent="invoice_summary")
    raise ValueError("Unsupported question; ask for invoice summary or outstanding invoices")


def compile_query(plan: QueryPlan, *, company_id: UUID, as_of: date):
    if not isinstance(company_id, UUID):
        raise ValueError("Server-resolved company scope is required")
    # Revalidate even when callers bypass Pydantic with model_construct.
    plan = QueryPlan.model_validate(plan.model_dump())
    # Historical amounts come from successful payment events through cutoff,
    # never today's amount_paid. UTC matches the source replay clearing dates.
    stop = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), timezone.utc)
    paid = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.invoice_id == Invoice.id, Payment.status == PaymentStatus.SUCCESS,
        Payment.currency == Invoice.currency, Payment.payment_date < stop,
    ).correlate(Invoice).scalar_subquery()
    outstanding = func.greatest(Invoice.amount_total - paid, 0)
    eligible = Invoice.status.notin_([InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF])
    posted = func.coalesce(Invoice.posting_date, Invoice.issue_date) <= as_of
    if plan.intent == "invoice_summary":
        # Currency groups prevent invalid totals across mixed currencies.
        return select(Invoice.currency, func.count(Invoice.id).label("invoice_count"),
                      func.sum(outstanding).label("outstanding_amount")).where(
            Invoice.company_id == company_id, posted, eligible,
        ).group_by(Invoice.currency).limit(plan.limit)
    return select(Invoice.invoice_number, Invoice.currency, Invoice.due_date,
                  outstanding.label("outstanding_amount")).where(
        Invoice.company_id == company_id, posted, eligible, outstanding > 0,
    ).order_by(Invoice.due_date, Invoice.id).limit(plan.limit)


async def investigate_question(session_factory, *, question: str, company_id: UUID, as_of: date,
                               authorized_company_ids: tuple[UUID, ...] = ()) -> dict:
    if not isinstance(company_id, UUID) or company_id not in authorized_company_ids:
        raise PermissionError("Investigation company is outside authorized scope")
    plan = plan_question(question)
    statement = compile_query(plan, company_id=company_id, as_of=as_of)

    async def execute():
        # A dedicated transaction avoids setting the mutable workflow transaction read-only.
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                await session.execute(text("SET LOCAL statement_timeout = '1500ms'"))
                rows = (await session.execute(statement)).mappings().all()
                return [dict(row) for row in rows]

    rows = await asyncio.wait_for(execute(), timeout=2)
    return {"mode": "constrained_deterministic_nl2sql", "intent": plan.intent,
            "scope": "case_company", "rows": rows,
            "explanation": "Payment-event balances through the UTC cutoff for the authorized case company; current cancelled/written-off invoices excluded. Missing payment history is not inferred from current balances; past status changes are not reconstructed."}
