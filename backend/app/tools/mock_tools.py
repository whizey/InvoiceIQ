"""Mock action tools: simulate the recovery agent's real-world actions.

Intentionally simple, DB-writing simulations — not real integrations. Built
behind this interface so Stripe/Resend/Twilio can replace them later
without touching the graph or policy logic (see docs/architecture.md).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from app.core.time import utc_today

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CommunicationLog, Contact, Invoice, PromiseToPay, RecoveryCase
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationStatus,
    PromiseToPayStatus,
    RecoveryActionType,
)

PROMISE_TO_PAY_DEFAULT_DAYS = 7


async def send_email(
    session: AsyncSession, case: RecoveryCase, invoice: Invoice, contact: Contact | None
) -> dict:
    subject = f"Invoice {invoice.invoice_number} overdue"
    greeting = f"Hi {contact.name}" if contact else "Hi"
    body = (
        f"{greeting}, Invoice {invoice.invoice_number} (₹{invoice.amount_total:,.2f}) "
        f"was due {invoice.due_date.isoformat()} and remains unpaid."
    )
    session.add(
        CommunicationLog(
            recovery_case_id=case.id,
            contact_id=contact.id if contact else None,
            channel=CommunicationChannel.EMAIL,
            direction=CommunicationDirection.OUTBOUND,
            subject=subject,
            body=body,
            status=CommunicationStatus.SIMULATED,
            sent_at=datetime.now(timezone.utc),
        )
    )
    return {"channel": "email", "simulated": True, "to": contact.email if contact else None}


async def generate_payment_link(
    session: AsyncSession, case: RecoveryCase, invoice: Invoice, contact: Contact | None
) -> dict:
    link = f"https://pay.example.com/mock/{invoice.invoice_number.lower()}"
    session.add(
        CommunicationLog(
            recovery_case_id=case.id,
            contact_id=contact.id if contact else None,
            channel=CommunicationChannel.EMAIL,
            direction=CommunicationDirection.OUTBOUND,
            subject=f"Payment link for Invoice {invoice.invoice_number}",
            body=f"Please use this link to settle the outstanding balance: {link}",
            status=CommunicationStatus.SIMULATED,
            sent_at=datetime.now(timezone.utc),
        )
    )
    return {"payment_link": link, "simulated": True}


async def record_promise_to_pay(session: AsyncSession, case: RecoveryCase, invoice: Invoice) -> dict:
    promised_date = utc_today() + timedelta(
    days=PROMISE_TO_PAY_DEFAULT_DAYS
)
    outstanding: Decimal = invoice.amount_total - invoice.amount_paid
    session.add(
        PromiseToPay(
            recovery_case_id=case.id,
            invoice_id=invoice.id,
            promised_amount=outstanding,
            promised_date=promised_date,
            status=PromiseToPayStatus.PENDING,
        )
    )
    return {
        "promised_amount": str(outstanding),
        "promised_date": promised_date.isoformat(),
        "simulated": True,
    }


async def escalate_case() -> dict:
    return {"escalated_to": "finance-team@example.com", "simulated": True}


async def execute_mock_action(
    session: AsyncSession,
    action_type: RecoveryActionType,
    case: RecoveryCase,
    invoice: Invoice,
    contact: Contact | None,
) -> dict:
    if action_type == RecoveryActionType.SEND_EMAIL:
        return await send_email(session, case, invoice, contact)
    if action_type == RecoveryActionType.SEND_PAYMENT_LINK:
        return await generate_payment_link(session, case, invoice, contact)
    if action_type == RecoveryActionType.TRACK_PROMISE_TO_PAY:
        return await record_promise_to_pay(session, case, invoice)
    if action_type == RecoveryActionType.ESCALATE:
        return await escalate_case()
    if action_type == RecoveryActionType.WAIT:
        return {"simulated": True, "note": "no action taken this cycle"}
    if action_type == RecoveryActionType.CLOSE_CASE:
        return {"simulated": True, "note": "case closed without recovery"}
    raise ValueError(f"Unknown action type: {action_type}")
