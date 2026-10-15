"""Real disposable-PostgreSQL tenant isolation, with rollback-safe cleanup."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api.portal import issue
from app.core.db import engine, async_session_factory
from app.main import app
from app.models.portal import PortalWorkspace, PortalInvoice, PortalCaseEvent


@pytest.mark.asyncio
async def test_portal_workspace_isolation_and_showcase_action_scoping(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "true")
    await engine.dispose()
    alpha = uuid.uuid4()
    beta = uuid.uuid4()
    invoice_ids = []
    try:
        async with async_session_factory() as db:
            for wid, name in [(alpha, "Tenant Alpha"), (beta, "Tenant Beta")]:
                db.add(PortalWorkspace(id=wid, name=name, kind="showcase"))
                inv = PortalInvoice(workspace_id=wid, invoice_number="INV-001",
                                    counterparty="Scoped counterparty", issue_date=date.today(),
                                    posting_date=date.today(), due_date=date.today() + timedelta(days=30),
                                    amount_total=Decimal("2000.00"), amount_paid=Decimal("500.00"))
                db.add(inv)
                await db.flush()
                invoice_ids.append(inv.id)
            await db.commit()
            token_a = (await issue(db, alpha, None))["token"]
            token_b = (await issue(db, beta, None))["token"]

        # Both extra showcase tenants are deliberately invalid; only the
        # configured SHOWCASE_ID is accepted. No cross-account fallback.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://ci") as client:
            for token in (token_a, token_b):
                headers = {"Authorization": "Bearer " + token}
                response = await client.get("/portal/overview", headers=headers)
                assert response.status_code == 403
            legacy = await client.get("/companies")
            assert legacy.status_code == 403
            legacy_write = await client.post("/recovery-cases/detect-overdue")
            assert legacy_write.status_code == 403
            # An unknown session cannot write a decision into another workspace.
            unauthorized = await client.post("/portal/decisions", headers={"Authorization":"Bearer iqp_invalid"},
                json={"invoice_id":str(invoice_ids[0]),"decision":"APPROVE","note":"Unauthorised action"})
            assert unauthorized.status_code == 401

        async with async_session_factory() as db:
            count = await db.scalar(__import__("sqlalchemy").select(__import__("sqlalchemy").func.count())
                                    .select_from(PortalCaseEvent).where(PortalCaseEvent.workspace_id.in_([alpha, beta])))
            assert count == 0
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_([alpha, beta])))
            await db.commit()
        await engine.dispose()
