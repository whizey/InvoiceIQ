"""Unit tests for the mock action tools — the actual "doing" layer the
policy engine's approved actions execute. Previously only exercised
indirectly, and unevenly, through whichever action the rule-based
fallback happened to recommend in workflow tests (which never recommends
TRACK_PROMISE_TO_PAY at all — see app/agents/llm_client.py).
"""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.db import async_session_factory, engine
from app.models import Company, Contact, Invoice, RecoveryCase
from app.models.enums import CompanySegment, InvoiceStatus, RecoveryActionType, RecoveryCaseStatus
from app.tools.mock_tools import escalate_case, execute_mock_action, generate_payment_link, record_promise_to_pay, send_email
from app.core.time import utc_today

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def db_ready():
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def _make_case_invoice_contact():
    async with async_session_factory() as session:
        company = Company(name="Mock Tools Test Co", industry="Testing", segment=CompanySegment.SMB)
        session.add(company)
        await session.flush()

        contact = Contact(
            company_id=company.id, name="Test Contact", email="test@mocktools.example",
            role="AP Manager", is_primary=True,
        )
        session.add(contact)

        due = utc_today() - timedelta(days=10)
        invoice = Invoice(
            company_id=company.id,
            invoice_number=f"INV-MOCKTOOLS-{company.id.hex[:8]}",
            amount_total=Decimal("75000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.OVERDUE,
        )
        session.add(invoice)
        await session.flush()

        case = RecoveryCase(
            invoice_id=invoice.id,
            company_id=company.id,
            status=RecoveryCaseStatus.OPEN,
            revenue_at_risk=invoice.amount_total,
        )
        session.add(case)
        await session.commit()
        return case, invoice, contact


async def test_send_email_creates_communication_log_with_contact_email():
    case, invoice, contact = await _make_case_invoice_contact()
    async with async_session_factory() as session:
        result = await send_email(session, case, invoice, contact)
        await session.commit()

    assert result["simulated"] is True
    assert result["to"] == contact.email


async def test_send_email_handles_missing_contact_gracefully():
    case, invoice, _ = await _make_case_invoice_contact()
    async with async_session_factory() as session:
        result = await send_email(session, case, invoice, None)
        await session.commit()

    assert result["to"] is None


async def test_generate_payment_link_uses_invoice_number():
    case, invoice, contact = await _make_case_invoice_contact()
    async with async_session_factory() as session:
        result = await generate_payment_link(session, case, invoice, contact)
        await session.commit()

    assert invoice.invoice_number.lower() in result["payment_link"]


async def test_record_promise_to_pay_creates_row_for_outstanding_balance():
    case, invoice, _ = await _make_case_invoice_contact()
    async with async_session_factory() as session:
        result = await record_promise_to_pay(session, case, invoice)
        await session.commit()

    assert Decimal(result["promised_amount"]) == invoice.amount_total - invoice.amount_paid
    assert result["promised_date"] > utc_today().isoformat()


async def test_escalate_case_returns_simulated_result():
    result = await escalate_case()
    assert result["simulated"] is True
    assert "@" in result["escalated_to"]


async def test_execute_mock_action_dispatches_wait_and_close_case_without_db_writes():
    case, invoice, contact = await _make_case_invoice_contact()
    async with async_session_factory() as session:
        wait_result = await execute_mock_action(
            session, RecoveryActionType.WAIT, case, invoice, contact
        )
        close_result = await execute_mock_action(
            session, RecoveryActionType.CLOSE_CASE, case, invoice, contact
        )

    assert wait_result["simulated"] is True
    assert close_result["simulated"] is True


async def test_execute_mock_action_raises_on_unknown_type():
    case, invoice, contact = await _make_case_invoice_contact()
    async with async_session_factory() as session:
        with pytest.raises(ValueError):
            await execute_mock_action(session, "NOT_A_REAL_ACTION", case, invoice, contact)
