"""Reminder drafts use only the case's facts; the policy gates contact; the log tracks what followed."""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import reminders

TODAY = date(2026, 10, 23)
NOW = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)


def invoice(total="145000", paid="0", due_days_ago=45):
    return SimpleNamespace(id=uuid.uuid4(), invoice_number="KIS-2026-0538", counterparty="Sai Ram Hardware Stores",
                           posting_date=TODAY - timedelta(days=due_days_ago + 30),
                           due_date=TODAY - timedelta(days=due_days_ago),
                           amount_total=Decimal(total), amount_paid=Decimal(paid))


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def ainvoke(self, messages):
        return SimpleNamespace(content=self.replies.pop(0))


def test_indian_grouping():
    assert reminders.rupees(Decimal("12000000")) == "₹1,20,00,000"
    assert reminders.rupees(145000) == "₹1,45,000" and reminders.rupees(999) == "₹999"


def test_templates_in_both_languages_carry_only_the_facts():
    facts = reminders.facts(invoice(paid="45000"), "Kaveri Industrial Supplies", TODAY)
    for channel in reminders.CHANNELS:
        for language in reminders.LANGUAGES:
            text = reminders.template(facts, channel, language)
            assert "₹1,00,000" in text and "KIS-2026-0538" in text and reminders.stray_figures(text, facts) == []
    assert reminders.template(facts, "email", "english").startswith("Subject:")


@pytest.mark.asyncio
async def test_a_draft_with_an_invented_number_is_rejected_then_the_retry_is_used():
    facts = reminders.facts(invoice(), "Kaveri", TODAY)
    model = Scripted("Please pay ₹1,50,000 by Friday, or 18% interest applies.",
                     "Dear team, ₹1,45,000 on invoice KIS-2026-0538 is 45 days overdue. When can we expect it?")
    result = await reminders.draft(facts, "whatsapp", "english", [(model, "test-model")])
    assert result["source"] == "test-model" and "45 days" in result["body"]
    assert "₹1,50,000" in result["rejected"] and "18%" in result["rejected"]


@pytest.mark.asyncio
async def test_falls_back_to_the_template_when_every_draft_strays():
    facts = reminders.facts(invoice(), "Kaveri", TODAY)
    model = Scripted("Pay ₹2 lakh now.", "Pay within 7 days.")
    result = await reminders.draft(facts, "email", "hinglish", [(model, "test-model")])
    assert result["source"] == "template" and result["body"].startswith("Subject:")


def test_policy_disputes_cool_off_and_escalation():
    assert reminders.policy(invoice(), "dispute", None, NOW)["blocked"].startswith("On hold")
    recent = reminders.policy(invoice(), None, NOW - timedelta(days=1), NOW)
    assert not recent["allowed"] and "25 Oct" in recent["blocked"]
    assert reminders.policy(invoice(), None, NOW - timedelta(days=3), NOW)["allowed"]
    assert "manager" in reminders.policy(invoice(total="600000"), None, None, NOW)["escalate"]
    assert "60 days" in reminders.policy(invoice(due_days_ago=75), None, None, NOW)["escalate"]
    assert reminders.policy(invoice(), None, None, NOW)["escalate"] is None
    assert not reminders.policy(invoice(paid="145000"), None, None, NOW)["allowed"]


def test_outcome_is_the_first_payment_within_two_weeks():
    pays = [SimpleNamespace(paid_on=TODAY + timedelta(days=20), amount=Decimal(1)),
            SimpleNamespace(paid_on=TODAY + timedelta(days=5), amount=Decimal(50000))]
    assert reminders.outcome(NOW, pays) == {"paid_on": "2026-10-28", "amount": "50000", "days_after": 5}
    assert reminders.outcome(NOW, pays[:1]) is None


@pytest.mark.asyncio
async def test_draft_send_and_cool_off_through_the_api(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete

    from app.core.db import async_session_factory, engine
    from app.main import app
    from app.models.portal import PortalUser, PortalWorkspace

    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setattr("app.agents.llm_client.gemini_fallback_chats", lambda timeout=None: [])
    await engine.dispose()
    today = date.today()
    item = {"invoice_number": "R-1", "counterparty": "Late Co", "issue_date": str(today - timedelta(days=60)),
            "posting_date": str(today - timedelta(days=60)), "due_date": str(today - timedelta(days=30)),
            "amount_total": "80000", "amount_paid": "0", "payment_date": None}
    workspace_id = user_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            reg = await client.post("/portal/register", json={
                "email": f"remind-{uuid.uuid4().hex}@example.invalid", "full_name": "Remind Owner",
                "company": "Remind Co", "password": "A-strong-unique-long-password-37"})
            headers = {"Authorization": f"Bearer {reg.json()['token']}"}
            me = (await client.get("/portal/me", headers=headers)).json()
            workspace_id, user_id = uuid.UUID(me["workspace_id"]), uuid.UUID(me["user_id"])
            assert (await client.post("/portal/import", json={"invoices": [item]}, headers=headers)).status_code == 201
            invoice_id = (await client.get("/portal/invoices", headers=headers)).json()["invoices"][0]["id"]
            ask = {"invoice_id": invoice_id, "channel": "whatsapp", "language": "hinglish"}
            drafted = (await client.post("/portal/reminder-draft", json=ask, headers=headers)).json()
            assert drafted["policy"]["allowed"] and drafted["draft"]["source"] == "template"
            assert "₹80,000" in drafted["draft"]["body"] and "R-1" in drafted["draft"]["body"]
            sent = {**ask, "body": drafted["draft"]["body"], "draft_source": "template"}
            assert (await client.post("/portal/reminders", json=sent, headers=headers)).status_code == 201
            again = await client.post("/portal/reminders", json=sent, headers=headers)
            assert again.status_code == 409 and "next reminder" in again.json()["detail"]
            blocked = (await client.post("/portal/reminder-draft", json=ask, headers=headers)).json()
            assert not blocked["policy"]["allowed"] and blocked["draft"] is None
            log = (await client.get("/portal/reminders", headers=headers)).json()
            assert log["summary"]["sent"] == 1 and log["reminders"][0]["payment"] is None
    finally:
        async with async_session_factory() as db:
            if workspace_id:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            if user_id:
                await db.execute(delete(PortalUser).where(PortalUser.id == user_id))
            await db.commit()
        await engine.dispose()
