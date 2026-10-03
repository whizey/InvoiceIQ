"""POST /recovery-cases/{id}/send-reminder-email — never sends anywhere but
settings.demo_notify_email, always goes through evaluate_policy() with the
same reminder cap/cooldown as an automated cycle, and errors clearly (400)
rather than guessing an address when DEMO_NOTIFY_EMAIL is unset.
"""

import asyncio
import contextlib
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.main import app
from app.models import Company, Contact, Invoice
from app.models.enums import CompanySegment, InvoiceStatus
from app.core.time import utc_today

NIL_UUID = "00000000-0000-0000-0000-000000000000"
FAKE_TO = "talepa.rahul6@gmail.com"


@contextlib.contextmanager
def api_client():
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


async def _create_overdue_case_async(invoice_number: str, days_overdue: int = 5) -> str:
    async with async_session_factory() as session:
        company = Company(name=f"Email Test Co {invoice_number}", industry="Testing", segment=CompanySegment.SMB)
        session.add(company)
        await session.flush()
        session.add(Contact(company_id=company.id, name="Fake Contact", email="fake@example.com", is_primary=True))

        due = utc_today() - timedelta(days=days_overdue)
        invoice = Invoice(
            company_id=company.id,
            invoice_number=invoice_number,
            amount_total=Decimal("50000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.SENT,
        )
        session.add(invoice)
        await session.commit()
        return str(invoice.id)


def create_overdue_case(invoice_number: str, days_overdue: int = 5) -> str:
    asyncio.run(engine.dispose())
    invoice_id = asyncio.run(_create_overdue_case_async(invoice_number, days_overdue))
    asyncio.run(engine.dispose())
    return invoice_id


def detect_and_get_case_id(client: TestClient, invoice_id: str) -> str:
    resp = client.post("/recovery-cases/detect-overdue")
    assert resp.status_code == 200
    cases = client.get("/recovery-cases").json()
    invoice = client.get(f"/invoices/{invoice_id}").json()
    return next(c["id"] for c in cases if c["invoice_number"] == invoice["invoice_number"])


def test_400_when_demo_notify_email_unset(monkeypatch):
    monkeypatch.setattr(settings, "demo_notify_email", None)
    invoice_id = create_overdue_case("INV-EMAILTEST-A001")
    with api_client() as client:
        case_id = detect_and_get_case_id(client, invoice_id)
        resp = client.post(f"/recovery-cases/{case_id}/send-reminder-email")
    assert resp.status_code == 400
    assert "DEMO_NOTIFY_EMAIL" in resp.json()["detail"]


def test_404_for_missing_case(monkeypatch):
    monkeypatch.setattr(settings, "demo_notify_email", FAKE_TO)
    with api_client() as client:
        resp = client.post(f"/recovery-cases/{NIL_UUID}/send-reminder-email")
    assert resp.status_code == 404


def test_first_reminder_is_simulated_and_never_targets_the_fake_contact(monkeypatch):
    monkeypatch.setattr(settings, "demo_notify_email", FAKE_TO)
    invoice_id = create_overdue_case("INV-EMAILTEST-B001")

    with api_client() as client:
        case_id = detect_and_get_case_id(client, invoice_id)
        resp = client.post(f"/recovery-cases/{case_id}/send-reminder-email")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "SIMULATED"  # no RESEND_API_KEY configured in tests
        assert body["to"] == FAKE_TO
        assert body["policy_decision"] == "APPROVED"

        detail = client.get(f"/recovery-cases/{case_id}").json()
        comm_logs = [log for log in detail["communication_logs"] if log["channel"] == "EMAIL"]
        assert len(comm_logs) == 1
        assert comm_logs[0]["recipient_email"] == FAKE_TO
        assert comm_logs[0]["recipient_email"] != "fake@example.com"

        action = next(a for a in detail["actions"] if a["action_type"] == "SEND_EMAIL")
        assert action["proposed_by"] == "HUMAN"
        assert action["recommended_action_type"] == "SEND_EMAIL"


def test_real_send_when_resend_configured(monkeypatch):
    monkeypatch.setattr(settings, "demo_notify_email", FAKE_TO)
    monkeypatch.setattr(settings, "resend_api_key", "re_fake")
    monkeypatch.setattr(settings, "resend_from", "demo@example.com")
    invoice_id = create_overdue_case("INV-EMAILTEST-C001")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with api_client() as client:
            case_id = detect_and_get_case_id(client, invoice_id)
            resp = client.post(f"/recovery-cases/{case_id}/send-reminder-email")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SENT"
    assert body["to"] == FAKE_TO


def test_cooldown_blocks_a_second_immediate_send(monkeypatch):
    monkeypatch.setattr(settings, "demo_notify_email", FAKE_TO)
    invoice_id = create_overdue_case("INV-EMAILTEST-D001")

    with api_client() as client:
        case_id = detect_and_get_case_id(client, invoice_id)
        first = client.post(f"/recovery-cases/{case_id}/send-reminder-email")
        assert first.status_code == 200
        assert first.json()["status"] == "SIMULATED"

        second = client.post(f"/recovery-cases/{case_id}/send-reminder-email")
        assert second.status_code == 200
        second_body = second.json()
        # Cooldown not elapsed -> policy substitutes WAIT, no email sent.
        assert second_body["status"] == "REJECTED"
        assert second_body["to"] is None

        detail = client.get(f"/recovery-cases/{case_id}").json()
        email_logs = [log for log in detail["communication_logs"] if log["channel"] == "EMAIL"]
        assert len(email_logs) == 1  # only the first send actually logged an email
