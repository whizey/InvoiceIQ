"""Deterministic revenue-at-risk detection and recovery-case creation.

Detecting overdue invoices and opening a case is a bookkeeping decision, not
a judgment call, so it stays deterministic — this is the "Event -> Revenue
Risk Assessment -> Recovery Case" stage from the architecture. Once a case
exists, it's immediately scored by the XGBoost model (app/ml/risk_model.py,
trained on synthetic data — see that module for the "not a production
financial model" caveat) — that's the "Context Gathering -> Risk/Probability
Model" stage. AI diagnosis/recommendation (Phase 8/9) still attach later.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from app.core.time import utc_today

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import get_publisher
from app.events import topics
from app.ml.risk_model import score as score_risk
from app.models import AuditLog, Invoice, PaymentEvent, RecoveryCase
from app.models.enums import AuditActor, InvoiceStatus, PaymentEventType, RecoveryCaseStatus
from app.services.promise_tracking import PromiseCheckResult, check_promises_to_pay
from app.services.risk_context import build_risk_features

# TODO(Phase 10): move into the formal deterministic policy engine config,
# alongside MAX_EMAIL_REMINDERS, HIGH_VALUE_THRESHOLD, etc.
MAX_RECOVERY_DAYS = 90

logger = logging.getLogger("app.risk_engine")


@dataclass
class DetectionResult:
    invoices_marked_overdue: list[Invoice]
    cases_created: list[RecoveryCase]
    promises_checked: PromiseCheckResult


async def mark_overdue_invoices(session: AsyncSession, *, today: date | None = None) -> list[Invoice]:
    """Flip SENT/PARTIALLY_PAID invoices whose due date has passed to OVERDUE."""
    today = today or utc_today()
    stmt = select(Invoice).where(
        Invoice.due_date < today,
        Invoice.status.in_([InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID]),
    )
    result = await session.execute(stmt)
    invoices = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    for invoice in invoices:
        invoice.status = InvoiceStatus.OVERDUE
        session.add(
            PaymentEvent(
                invoice_id=invoice.id,
                event_type=PaymentEventType.INVOICE_OVERDUE,
                payload={"due_date": invoice.due_date.isoformat()},
                occurred_at=now,
            )
        )
    await session.flush()
    return invoices


async def create_recovery_cases_for_overdue_invoices(session: AsyncSession) -> list[RecoveryCase]:
    """Create a recovery case for every OVERDUE invoice that doesn't already have one."""
    stmt = (
        select(Invoice)
        .outerjoin(RecoveryCase, RecoveryCase.invoice_id == Invoice.id)
        .where(Invoice.status == InvoiceStatus.OVERDUE, RecoveryCase.id.is_(None))
    )
    result = await session.execute(stmt)
    invoices = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    created: list[RecoveryCase] = []
    for invoice in invoices:
        revenue_at_risk = invoice.amount_total - invoice.amount_paid
        case = RecoveryCase(
            invoice_id=invoice.id,
            company_id=invoice.company_id,
            status=RecoveryCaseStatus.OPEN,
            opened_at=now,
            revenue_at_risk=revenue_at_risk,
            recovery_window_deadline=invoice.due_date + timedelta(days=MAX_RECOVERY_DAYS),
        )
        session.add(case)
        await session.flush()

        session.add(
            AuditLog(
                recovery_case_id=case.id,
                entity_type="recovery_case",
                entity_id=case.id,
                event_type="CASE_CREATED",
                actor=AuditActor.SYSTEM,
                description=(
                    f"Recovery case opened for overdue invoice {invoice.invoice_number} "
                    f"(₹{revenue_at_risk:,.2f} at risk)."
                ),
                occurred_at=now,
            )
        )

        features = await build_risk_features(session, invoice)
        risk_result = score_risk(features)
        case.risk_score = Decimal(str(risk_result.risk_score))
        case.risk_level = risk_result.risk_level
        case.recovery_probability = Decimal(str(risk_result.recovery_probability))

        session.add(
            AuditLog(
                recovery_case_id=case.id,
                entity_type="recovery_case",
                entity_id=case.id,
                event_type="RISK_SCORED",
                actor=AuditActor.SYSTEM,
                description=(
                    f"Risk scored via ML model (synthetic training data): "
                    f"{risk_result.risk_level.value} ({risk_result.risk_score}), "
                    f"recovery probability {risk_result.recovery_probability:.0%}."
                ),
                occurred_at=now,
            )
        )

        created.append(case)

    await session.flush()
    return created


async def run_detection(session: AsyncSession, *, today: date | None = None) -> DetectionResult:
    """Run the full deterministic housekeeping pass: mark overdue invoices, open
    cases for them, and resolve any pending promise-to-pay commitments whose
    date has come and gone."""
    logger.info("detection sweep starting")
    invoices_marked = await mark_overdue_invoices(session, today=today)
    cases_created = await create_recovery_cases_for_overdue_invoices(session)
    promises_checked = await check_promises_to_pay(session, today=today)
    await session.commit()
    logger.info(
        "detection sweep finished",
        extra={
            "invoices_marked_overdue": len(invoices_marked),
            "cases_created": len(cases_created),
            "promises_fulfilled": len(promises_checked.fulfilled),
            "promises_broken": len(promises_checked.broken),
        },
    )

    # Publish only after the commit succeeds — the DB write is the source of
    # truth, Kafka is a best-effort broadcast on top of it.
    publisher = get_publisher()
    for invoice in invoices_marked:
        await publisher.publish(
            topics.INVOICE_OVERDUE,
            str(invoice.id),
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "company_id": str(invoice.company_id),
                "due_date": invoice.due_date.isoformat(),
                "amount_total": str(invoice.amount_total),
            },
        )
    for case in cases_created:
        await publisher.publish(
            topics.RECOVERY_CASE_CREATED,
            str(case.id),
            {
                "case_id": str(case.id),
                "invoice_id": str(case.invoice_id),
                "company_id": str(case.company_id),
                "revenue_at_risk": str(case.revenue_at_risk),
            },
        )
    for promise in promises_checked.broken:
        await publisher.publish(
            topics.PROMISE_TO_PAY_BROKEN,
            str(promise.id),
            {
                "promise_id": str(promise.id),
                "recovery_case_id": str(promise.recovery_case_id),
                "promised_amount": str(promise.promised_amount),
                "promised_date": promise.promised_date.isoformat(),
            },
        )

    return DetectionResult(
        invoices_marked_overdue=invoices_marked,
        cases_created=cases_created,
        promises_checked=promises_checked,
    )
