"""Portal decisions flow through domain events into approval alerts, scoped per workspace."""
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock

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


async def escalate_one(client, tag, name, number, publisher, monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", "broker.invalid:9092")
    monkeypatch.setattr("app.api.portal.get_publisher", lambda: publisher)
    today = date.today()
    headers, workspace_id, user_id = await register(client, tag, name)
    await client.post("/portal/import", headers=headers, json={"invoices": [{
        "invoice_number": number, "counterparty": "Broker Co", "issue_date": str(today - timedelta(days=60)),
        "posting_date": str(today - timedelta(days=60)), "due_date": str(today - timedelta(days=20)),
        "amount_total": "500.00", "amount_paid": "0.00"}]})
    invoice_id = (await client.get("/portal/invoices", headers=headers)).json()["invoices"][0]["id"]
    response = await client.post("/portal/decisions", headers=headers, json={
        "invoice_id": invoice_id, "decision": "ESCALATE", "note": "Broker path check"})
    assert response.status_code == 201, response.text
    return headers, workspace_id, user_id


async def cleanup(workspace_ids, user_ids):
    async with async_session_factory() as db:
        await db.execute(delete(ApprovalAlert).where(ApprovalAlert.workspace_id.in_(workspace_ids)))
        await db.execute(delete(OutboxEvent).where(
            OutboxEvent.payload["data"]["workspace_id"].astext.in_([str(w) for w in workspace_ids])))
        await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
        await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
        await db.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_with_a_reachable_broker_the_event_travels_through_it_and_the_consumer_applies_it(monkeypatch):
    from app.events.handlers import handle_message
    await engine.dispose()
    publisher = AsyncMock()
    publisher.delivery_mode = "kafka"
    ids = ([], [])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await escalate_one(client, uuid.uuid4().hex, "Broker A", "INV-K", publisher, monkeypatch)
            ids[0].append(workspace_id)
            ids[1].append(user_id)
            publisher.publish_reliably.assert_awaited_once()
            assert publisher.publish_reliably.await_args.args[0] == "review.requested"
            assert (await client.get("/portal/alerts", headers=headers)).json()["alerts"] == []   # consumer has not run yet
            async with async_session_factory() as db:
                row = (await db.scalars(select(OutboxEvent).where(
                    OutboxEvent.payload["data"]["workspace_id"].astext == str(workspace_id)))).one()
                assert row.published_at is not None and row.delivery_mode == "kafka"
                envelope = row.payload
            # What the consumer does when the broker hands it the message, including a redelivery.
            assert await handle_message(async_session_factory, "review.requested", envelope) == "processed"
            assert await handle_message(async_session_factory, "review.requested", envelope) == "duplicate"
            alerts = (await client.get("/portal/alerts", headers=headers)).json()
            assert alerts["open"] == 1 and alerts["alerts"][0]["delivered_via"] == "kafka"
    finally:
        await cleanup(ids[0], ids[1])


@pytest.mark.asyncio
async def test_with_an_unreachable_broker_the_event_is_applied_in_process(monkeypatch):
    await engine.dispose()
    publisher = AsyncMock()
    publisher.delivery_mode = "kafka"
    publisher.publish_reliably.side_effect = ConnectionError("broker unavailable")
    ids = ([], [])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await escalate_one(client, uuid.uuid4().hex, "Broker B", "INV-F", publisher, monkeypatch)
            ids[0].append(workspace_id)
            ids[1].append(user_id)
            alerts = (await client.get("/portal/alerts", headers=headers)).json()
            assert alerts["open"] == 1 and alerts["alerts"][0]["delivered_via"] == "inline"
    finally:
        await cleanup(ids[0], ids[1])
