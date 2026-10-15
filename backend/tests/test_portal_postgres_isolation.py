"""End-to-end portal tests: genuine disposable PostgreSQL and FastAPI routes.

The global test connection guard prevents running this suite against Neon,
production, development, or an unverified database.
"""
import uuid
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.db import async_session_factory, engine
from app.main import app
from app.models.portal import PortalWorkspace, PortalUser, PortalCaseEvent


@pytest.mark.asyncio
async def test_portal_customer_journeys_reconcile_and_isolate(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    await engine.dispose()
    tag = uuid.uuid4().hex
    email_a, email_b = f"alpha-{tag}@example.invalid", f"beta-{tag}@example.invalid"
    workspace_ids = []
    user_ids = []
    today = date.today()
    payload = {
        "invoices": [{
            "invoice_number": "INV-0001",
            "counterparty": "Verified Customer",
            "issue_date": str(today - timedelta(days=40)),
            "posting_date": str(today - timedelta(days=40)),
            "due_date": str(today - timedelta(days=10)),
            "amount_total": "2000.00",
            "amount_paid": "500.00",
            "payment_date": str(today - timedelta(days=5)),
        }]
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            sessions = []
            for address, name in [(email_a, "Alpha Finance"), (email_b, "Beta Finance")]:
                registered = await client.post("/portal/register", json={
                    "email": address, "full_name": "Test Owner", "company": name,
                    "password": "A-strong-unique-long-password-37",
                })
                assert registered.status_code == 201, registered.text
                token = registered.json()["token"]
                assert token.startswith("iqp_")
                sessions.append({"Authorization": f"Bearer {token}"})
                me = await client.get("/portal/me", headers=sessions[-1])
                assert me.status_code == 200, me.text
                workspace_ids.append(uuid.UUID(me.json()["workspace_id"]))
                user_ids.append(uuid.UUID(me.json()["user_id"]))
            assert workspace_ids[0] != workspace_ids[1]

            # Invoice numbers may repeat across customers, but are unique
            # within one workspace. The two real SQL partitions cannot mix.
            for headers in sessions:
                imported = await client.post("/portal/import", json=payload, headers=headers)
                assert imported.status_code == 201, imported.text
                assert imported.json()["imported"] == 1
                duplicate = await client.post("/portal/import", json=payload, headers=headers)
                assert duplicate.status_code == 409

            overview_a = await client.get("/portal/overview", headers=sessions[0])
            overview_b = await client.get("/portal/overview", headers=sessions[1])
            assert overview_a.status_code == overview_b.status_code == 200
            assert overview_a.json()["workspace"] == "Alpha Finance"
            assert overview_b.json()["workspace"] == "Beta Finance"
            assert overview_a.json()["metrics"]["open"] == "1500.00"
            assert overview_a.json()["metrics"]["collected"] == "500.00"
            invoice_a = overview_a.json()["invoices"][0]["id"]
            invoice_b = overview_b.json()["invoices"][0]["id"]
            assert invoice_a != invoice_b

            financial = await client.get("/portal/dashboard", headers=sessions[0])
            assert financial.status_code == 200, financial.text
            doc = financial.json()
            assert doc["workspace"] == "Alpha Finance"
            assert doc["metrics"]["invoiced"] == "2000.00"
            assert doc["metrics"]["open"] == "1500.00"
            assert doc["metrics"]["overdue"] == "1500.00"
            assert doc["metrics"]["collected"] == "500.00"
            assert doc["metrics"]["scored_open"] == 0
            assert doc["metrics"]["invoice_count"] == 1
            assert doc["aging"]
            assert doc["risk"]
            assert doc["series"]
            assert doc["invoices"][0]["invoice_number"] == "INV-0001"

            # Cross-tenant mutations must be indistinguishable from an
            # unknown invoice, and leave both customer ledgers untouched.
            forbidden = await client.post("/portal/decisions", headers=sessions[0], json={
                "invoice_id": invoice_b, "decision": "APPROVE", "note": "Cross-tenant attempt",
            })
            assert forbidden.status_code == 404
            allowed = await client.post("/portal/decisions", headers=sessions[0], json={
                "invoice_id": invoice_a, "decision": "ESCALATE", "note": "Manual internal review",
            })
            assert allowed.status_code == 201, allowed.text
            assert allowed.json()["external_effects"] is False

            audit_a = await client.get("/portal/overview", headers=sessions[0])
            audit_b = await client.get("/portal/overview", headers=sessions[1])
            assert len(audit_a.json()["actions"]) == 1
            assert audit_a.json()["actions"][0]["decision"] == "ESCALATE"
            assert audit_b.json()["actions"] == []

            history = await client.get("/portal/dashboard",
                                       params={"as_of": str(today - timedelta(days=7))},
                                       headers=sessions[0])
            assert history.status_code == 200, history.text
            assert history.json()["metrics"]["open"] == "2000.00"
            assert history.json()["metrics"]["collected"] == "0"

            wrong = await client.post("/portal/login", json={
                "email": email_a, "password": "not-the-real-password",
            })
            assert wrong.status_code == 401
            login = await client.post("/portal/login", json={
                "email": email_a, "password": "A-strong-unique-long-password-37",
            })
            assert login.status_code == 200, login.text
            assert login.json()["kind"] == "customer"

            out = await client.delete("/portal/session", headers=sessions[0])
            assert out.status_code == 200
            revoked = await client.get("/portal/overview", headers=sessions[0])
            assert revoked.status_code == 401

            # Keep the previous operator APIs unavailable to portal visitors.
            assert (await client.get("/companies")).status_code == 403
            assert (await client.post("/recovery-cases/detect-overdue")).status_code == 403
            assert (await client.post("/portal/showcase")).status_code == 404
        async with async_session_factory() as db:
            n = await db.scalar(select(func.count()).select_from(PortalCaseEvent)
                                .where(PortalCaseEvent.workspace_id.in_(workspace_ids)))
            assert n == 1
    finally:
        # Parent deletes cascade to all test-scoped sessions, ledger rows and
        # audit events; only generated test UUIDs are eligible for cleanup.
        async with async_session_factory() as db:
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()
