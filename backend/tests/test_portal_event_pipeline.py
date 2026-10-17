"""Portal decisions flow through domain events into approval alerts, scoped per workspace."""
import uuid
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.main import app
from app.models import OutboxEvent
from app.models.event_effects import ApprovalAlert, ProcessedEvent
from app.models.portal import PortalUser, PortalWorkspace


async def register(client, tag, name):
    reg = await client.post("/portal/register", json={
        "email": f"{name[-1].lower()}-{tag}@example.invalid", "full_name": "Test Owner",
        "company": name, "password": "A-strong-unique-long-password-37"})
    assert reg.status_code == 201, reg.text
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}
    me = (await client.get("/portal/me", headers=headers)).json()
    return headers, uuid.UUID(me["workspace_id"]), uuid.UUID(me["user_id"])


@pytest.mark.asyncio
async def test_escalation_raises_an_alert_and_a_decision_resolves_it(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", None)
    await engine.dispose()
    tag, today = uuid.uuid4().hex, date.today()
    workspace_ids, user_ids = [], []
    invoice = {"invoices": [{"invoice_number": "INV-A", "counterparty": "Overdue Co",
                             "issue_date": str(today - timedelta(days=60)), "posting_date": str(today - timedelta(days=60)),
                             "due_date": str(today - timedelta(days=20)), "amount_total": "1000.00", "amount_paid": "0.00"}]}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            a, workspace_a, user_a = await register(client, tag, "Events A")
            b, workspace_b, user_b = await register(client, tag, "Events B")
            workspace_ids += [workspace_a, workspace_b]
            user_ids += [user_a, user_b]
            assert (await client.post("/portal/import", json=invoice, headers=a)).status_code == 201
            invoice_id = (await client.get("/portal/invoices?q=INV-A", headers=a)).json()["invoices"][0]["id"]

            assert (await client.get("/portal/alerts", headers=a)).json()["open"] == 0
            escalate = await client.post("/portal/decisions", headers=a, json={
                "invoice_id": invoice_id, "decision": "ESCALATE", "note": "Large overdue balance"})
            assert escalate.status_code == 201, escalate.text
            alerts = (await client.get("/portal/alerts", headers=a)).json()
            assert alerts["open"] == 1
            assert "INV-A" in alerts["alerts"][0]["subject"] and alerts["alerts"][0]["reason"] == "Large overdue balance"
            assert (await client.get("/portal/alerts", headers=b)).json()["alerts"] == []      # other tenant sees nothing

            approve = await client.post("/portal/decisions", headers=a, json={
                "invoice_id": invoice_id, "decision": "APPROVE", "note": "Owner confirmed plan"})
            assert approve.status_code == 201
            after = (await client.get("/portal/alerts", headers=a)).json()
            assert after["open"] == 0 and after["alerts"][0]["status"] == "RESOLVED"
            assert after["alerts"][0]["resolved_decision"] == "APPROVE"

            async with async_session_factory() as db:
                events = (await db.scalars(select(OutboxEvent).where(
                    OutboxEvent.payload["data"]["workspace_id"].astext == str(workspace_a)))).all()
                assert sorted(e.topic for e in events) == ["review.requested", "review.resolved"]
                assert {e.delivery_mode for e in events} == {"inline"} and all(e.published_at for e in events)
                assert await db.scalar(select(func.count()).select_from(ProcessedEvent).where(
                    ProcessedEvent.event_id.in_([e.id for e in events]))) == 2
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(ApprovalAlert).where(ApprovalAlert.workspace_id.in_(workspace_ids)))
            await db.execute(delete(OutboxEvent).where(OutboxEvent.payload["data"]["workspace_id"].astext.in_([str(w) for w in workspace_ids])))
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_with_a_broker_configured_the_decision_waits_for_the_consumer(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", "broker.invalid:9092")
    await engine.dispose()
    tag, today = uuid.uuid4().hex, date.today()
    workspace_ids, user_ids = [], []
    invoice = {"invoices": [{"invoice_number": "INV-Z", "counterparty": "Slow Co",
                             "issue_date": str(today - timedelta(days=60)), "posting_date": str(today - timedelta(days=60)),
                             "due_date": str(today - timedelta(days=20)), "amount_total": "500.00", "amount_paid": "0.00"}]}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            a, workspace_a, user_a = await register(client, tag, "Broker A")
            workspace_ids.append(workspace_a)
            user_ids.append(user_a)
            await client.post("/portal/import", json=invoice, headers=a)
            invoice_id = (await client.get("/portal/invoices", headers=a)).json()["invoices"][0]["id"]
            assert (await client.post("/portal/decisions", headers=a, json={
                "invoice_id": invoice_id, "decision": "ESCALATE", "note": "Waiting for broker"})).status_code == 201
            assert (await client.get("/portal/alerts", headers=a)).json()["alerts"] == []   # the consumer has not run
            async with async_session_factory() as db:
                pending = (await db.scalars(select(OutboxEvent).where(
                    OutboxEvent.payload["data"]["workspace_id"].astext == str(workspace_a)))).all()
                assert len(pending) == 1 and pending[0].published_at is None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(OutboxEvent).where(OutboxEvent.payload["data"]["workspace_id"].astext.in_([str(w) for w in workspace_ids])))
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()
