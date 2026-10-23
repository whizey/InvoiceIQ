"""Delete my data: owner only, confirmed, and nothing of the workspace left behind, event payloads included."""
import uuid
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.db import async_session_factory, engine
from app.main import app
from app.models.event_effects import ApprovalAlert
from app.models.outbox import OutboxEvent
from app.models.portal import PortalInvoice, PortalNote, PortalUser, PortalWorkspace


@pytest.mark.asyncio
async def test_owner_erases_data_then_account(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    today = date.today()
    item = {"invoice_number": "E-1", "counterparty": "Erase Co", "issue_date": str(today - timedelta(days=60)),
            "posting_date": str(today - timedelta(days=60)), "due_date": str(today - timedelta(days=30)),
            "amount_total": "50000", "amount_paid": "0", "payment_date": None}
    workspace_id = user_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            password = "A-strong-unique-long-password-37"
            reg = await client.post("/portal/register", json={
                "email": f"erase-{uuid.uuid4().hex}@example.invalid", "full_name": "Erase Owner",
                "company": "Erase Co Pvt Ltd", "password": password})
            headers = {"Authorization": f"Bearer {reg.json()['token']}"}
            me = (await client.get("/portal/me", headers=headers)).json()
            workspace_id, user_id = uuid.UUID(me["workspace_id"]), uuid.UUID(me["user_id"])
            await client.post("/portal/import", json={"invoices": [item]}, headers=headers)
            invoice_id = (await client.get("/portal/invoices", headers=headers)).json()["invoices"][0]["id"]
            await client.post("/portal/notes", json={"invoice_id": invoice_id, "kind": "internal",
                                                     "body": "Called, no answer."}, headers=headers)
            await client.post("/portal/decisions", json={"invoice_id": invoice_id, "decision": "ESCALATE",
                                                         "note": "Escalate this one."}, headers=headers)
            wrong = await client.post("/portal/erase", json={"scope": "data", "confirm": "Erase Co"}, headers=headers)
            assert wrong.status_code == 422
            done = await client.post("/portal/erase", json={"scope": "data", "confirm": "Erase Co Pvt Ltd"},
                                     headers=headers)
            assert done.status_code == 200 and done.json()["invoices"] == 1
            async with async_session_factory() as db:
                for model in (PortalInvoice, PortalNote, ApprovalAlert):
                    assert await db.scalar(select(func.count()).select_from(model).where(
                        model.workspace_id == workspace_id)) == 0
                assert await db.scalar(select(func.count()).select_from(OutboxEvent).where(
                    OutboxEvent.payload["data"]["workspace_id"].astext == str(workspace_id))) == 0
            bad = await client.post("/portal/erase", json={"scope": "account", "confirm": "Erase Co Pvt Ltd",
                                                           "password": "not-the-password-123"}, headers=headers)
            assert bad.status_code == 403
            gone = await client.post("/portal/erase", json={"scope": "account", "confirm": "Erase Co Pvt Ltd",
                                                            "password": password}, headers=headers)
            assert gone.status_code == 200
            assert (await client.get("/portal/me", headers=headers)).status_code == 401
            async with async_session_factory() as db:
                assert await db.get(PortalWorkspace, workspace_id) is None and await db.get(PortalUser, user_id) is None
            workspace_id = user_id = None
    finally:
        async with async_session_factory() as db:
            if workspace_id:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            if user_id:
                await db.execute(delete(PortalUser).where(PortalUser.id == user_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_public_workspace_cannot_be_erased():
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api import portal

    visitor = (SimpleNamespace(user_id=None, token_digest="t"), SimpleNamespace(id=uuid.uuid4(), kind="showcase",
                                                                                 name="Public"))
    with pytest.raises(HTTPException) as refused:
        await portal.erase(portal.EraseRequest(scope="data", confirm="Public"), subject=visitor, db=None)
    assert refused.value.status_code == 403
