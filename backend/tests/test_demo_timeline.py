"""GET /portal/demo-timeline: a chronological replay of a showcase workspace's own generated history --
invoices and payments already in the database, ordered, not fabricated fresh on each request."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api import portal
from app.core.db import async_session_factory, engine
from app.models.portal import PortalInvoice, PortalPayment, PortalWorkspace
from app.main import app


@pytest.mark.asyncio
async def test_demo_timeline_orders_invoices_and_payments_chronologically(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    workspace_id = uuid.uuid4()
    monkeypatch.setattr(portal, "SHOWCASE_ID", workspace_id)
    today = date.today()
    try:
        async with async_session_factory() as db:
            workspace = PortalWorkspace(id=workspace_id, name="Demo Co", kind="showcase")
            db.add(workspace)
            await db.flush()
            later = PortalInvoice(workspace_id=workspace_id, invoice_number="D-2", counterparty="Later Co",
                                  issue_date=today - timedelta(days=10), posting_date=today - timedelta(days=10),
                                  due_date=today + timedelta(days=20), amount_total=Decimal("2000.00"),
                                  amount_paid=Decimal("0"))
            earlier = PortalInvoice(workspace_id=workspace_id, invoice_number="D-1", counterparty="Earlier Co",
                                    issue_date=today - timedelta(days=30), posting_date=today - timedelta(days=30),
                                    due_date=today, amount_total=Decimal("1000.00"), amount_paid=Decimal("1000.00"))
            db.add_all([later, earlier])
            await db.flush()
            db.add(PortalPayment(workspace_id=workspace_id, invoice_id=earlier.id,
                                 paid_on=today - timedelta(days=5), amount=Decimal("1000.00")))
            await db.commit()
            token = (await portal.issue(db, workspace_id, None))["token"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/portal/demo-timeline",
                                        headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["workspace"] == "Demo Co" and body["total"] == 3
        kinds = [(e["kind"], e["invoice"]) for e in body["events"]]
        # Earlier invoice (D-1) first, then the later invoice (D-2), then the payment against D-1 -- real
        # chronological order, not creation order (D-2 was added to the session before the D-1 payment).
        assert kinds == [("invoice", "D-1"), ("invoice", "D-2"), ("payment", "D-1")]
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_demo_timeline_refuses_a_customer_workspace(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    tag = uuid.uuid4().hex
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            reg = await client.post("/portal/register", json={
                "email": f"owner-{tag}@example.invalid", "full_name": "Test Owner",
                "company": "Demo Timeline Refusal Co", "password": "A-strong-unique-long-password-37"})
            headers = {"Authorization": f"Bearer {reg.json()['token']}"}
            response = await client.get("/portal/demo-timeline", headers=headers)
            assert response.status_code == 403
    finally:
        await engine.dispose()
