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
from app.models.portal import PortalWorkspace, PortalUser, PortalCaseEvent, PortalInvoice
from app.api.portal import SHOWCASE_ID


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

            page_a = await client.get("/portal/invoices", headers=sessions[0],
                                      params={"q": "Verified", "page_size": 1})
            assert page_a.status_code == 200, page_a.text
            assert page_a.json()["total"] == 1
            assert [row["id"] for row in page_a.json()["invoices"]] == [invoice_a]
            assert (await client.get("/portal/invoices", headers=sessions[0],
                                     params={"q": "missing"})).json()["total"] == 0
            assert (await client.get("/portal/invoices", headers=sessions[0],
                                     params={"page_size": 101})).status_code == 422
            assert (await client.get("/portal/invoices")).status_code == 401

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
            assert doc["series"][-1]["open"] == "1500.00"
            assert doc["invoices"][0]["invoice_number"] == "INV-0001"

            scored = await client.post("/portal/score", headers=sessions[0])
            assert scored.status_code == 200, scored.text
            assert scored.json()["scored"] == 1
            intelligence_a = await client.get("/portal/intelligence", headers=sessions[0])
            intelligence_b = await client.get("/portal/intelligence", headers=sessions[1])
            assert intelligence_a.status_code == intelligence_b.status_code == 200
            assert intelligence_a.json()["scored"] == 1
            assert intelligence_b.json()["scored"] == 0
            explanation = await client.get(f"/portal/intelligence/{invoice_a}", headers=sessions[0])
            assert explanation.status_code == 200, explanation.text
            assert explanation.json()["explanation_method"] == "LightGBM native TreeSHAP"
            assert explanation.json()["contributions"]
            assert (await client.get(f"/portal/intelligence/{invoice_a}",
                                     headers=sessions[1])).status_code == 404
            assert (await client.post("/portal/score")).status_code == 401

            export_a = await client.get("/portal/export", headers=sessions[0])
            export_b = await client.get("/portal/export", headers=sessions[1])
            assert export_a.status_code == export_b.status_code == 200
            assert export_a.headers["content-type"].startswith("text/csv")
            assert export_a.text.count("INV-0001") == 1
            assert "Verified Customer" in export_a.text
            assert "Alpha Finance" not in export_b.text
            unauthorized_export = await client.get("/portal/export")
            assert unauthorized_export.status_code == 401

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
            assert history.json()["series"][-1]["open"] == "2000.00"

            settings = await client.get("/portal/settings", headers=sessions[0])
            assert settings.status_code == 200
            assert settings.json()["role"] == "owner"
            assert settings.json()["company"] == "Alpha Finance"
            assert (await client.get("/portal/settings", headers=sessions[1])).json()["company"] == "Beta Finance"
            renamed = await client.patch("/portal/settings", headers=sessions[0], json={
                "full_name": "Updated Owner", "company": "Alpha Finance Updated",
            })
            assert renamed.status_code == 200, renamed.text
            assert (await client.get("/portal/settings", headers=sessions[0])).json()["full_name"] == "Updated Owner"
            assert (await client.get("/portal/settings", headers=sessions[1])).json()["company"] == "Beta Finance"

            wrong = await client.post("/portal/login", json={
                "email": email_a, "password": "not-the-real-password",
            })
            assert wrong.status_code == 401
            login = await client.post("/portal/login", json={
                "email": email_a, "password": "A-strong-unique-long-password-37",
            })
            assert login.status_code == 200, login.text
            assert login.json()["kind"] == "customer"
            login_headers = {"Authorization": f"Bearer {login.json()['token']}"}
            changed = await client.patch("/portal/settings", headers=sessions[0], json={
                "current_password": "A-strong-unique-long-password-37",
                "new_password": "A-new-strong-unique-password-38",
            })
            assert changed.status_code == 200, changed.text
            assert (await client.get("/portal/me", headers=login_headers)).status_code == 401
            assert (await client.post("/portal/login", json={
                "email": email_a, "password": "A-strong-unique-long-password-37",
            })).status_code == 401
            assert (await client.post("/portal/login", json={
                "email": email_a, "password": "A-new-strong-unique-password-38",
            })).status_code == 200

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


@pytest.mark.asyncio
async def test_showcase_decisions_are_private_to_each_visitor(monkeypatch):
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "true")
    invoice_id = uuid.uuid4()
    today = date.today()
    async with async_session_factory() as db:
        assert await db.get(PortalWorkspace, SHOWCASE_ID) is None
        db.add(PortalWorkspace(id=SHOWCASE_ID, name="Isolated Showcase", kind="showcase"))
        await db.flush()
        db.add(PortalInvoice(id=invoice_id, workspace_id=SHOWCASE_ID,
                             invoice_number="SHOWCASE-001", counterparty="Illustrative Account",
                             issue_date=today, posting_date=today, due_date=today + timedelta(days=30),
                             amount_total=100, amount_paid=0))
        await db.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            first = await client.post("/portal/showcase")
            second = await client.post("/portal/showcase")
            assert first.status_code == second.status_code == 200
            first_headers = {"Authorization": f"Bearer {first.json()['token']}"}
            second_headers = {"Authorization": f"Bearer {second.json()['token']}"}
            recorded = await client.post("/portal/decisions", headers=first_headers, json={
                "invoice_id": str(invoice_id), "decision": "ESCALATE", "note": "Private visitor action",
            })
            assert recorded.status_code == 201, recorded.text
            assert len((await client.get("/portal/overview", headers=first_headers)).json()["actions"]) == 1
            assert (await client.get("/portal/overview", headers=second_headers)).json()["actions"] == []
            assert (await client.get("/portal/settings", headers=first_headers)).status_code == 403
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == SHOWCASE_ID))
            await db.commit()
        await engine.dispose()
