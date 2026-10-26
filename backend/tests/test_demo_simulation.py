"""POST /portal/onboarding-tick: a brand-new customer workspace's own guided walkthrough, playing through
Kaveri's deterministic sample story one event at a time, through the real pipeline, not loaded all at once."""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api import portal
from app.core.db import async_session_factory, engine
from app.main import app
from app.models.portal import PortalUser, PortalWorkspace


@pytest.mark.asyncio
async def test_onboarding_tick_writes_real_invoices_into_the_new_workspace_through_the_real_pipeline(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    workspace_id = user_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            reg = await client.post("/portal/register", json={
                "email": f"onboarding-{uuid.uuid4().hex}@example.invalid", "full_name": "New Owner",
                "company": "New Owner Co", "password": "A-strong-unique-long-password-37"})
            headers = {"Authorization": f"Bearer {reg.json()['token']}"}
            me = (await client.get("/portal/me", headers=headers)).json()
            workspace_id, user_id = uuid.UUID(me["workspace_id"]), uuid.UUID(me["user_id"])

            empty = await client.get("/portal/invoices", headers=headers)
            assert empty.json()["total"] == 0  # nothing written until the first tick

            tick = await client.post("/portal/onboarding-tick", headers=headers)
            assert tick.status_code == 200
            tick_body = tick.json()
            assert tick_body["cursor"] == 1 and len(tick_body["events"]) == 1
            first = tick_body["events"][0]
            assert first["kind"] == "invoice" and first["invoice"].startswith("KIS-")

            invoices = await client.get("/portal/invoices", headers=headers)
            assert invoices.json()["total"] == 1
            assert invoices.json()["invoices"][0]["number"] == first["invoice"]

            # A second tick with a larger batch advances further without re-writing the first event.
            tick2 = await client.post("/portal/onboarding-tick", params={"count": 5}, headers=headers)
            assert tick2.status_code == 200
            assert tick2.json()["cursor"] == 6
            invoices_after = await client.get("/portal/invoices", headers=headers)
            assert invoices_after.json()["total"] <= 6  # some of the 5 may be payment events, not new invoices
            numbers = {i["number"] for i in invoices_after.json()["invoices"]}
            assert first["invoice"] in numbers and len(numbers) == len(set(numbers))  # no duplicates
    finally:
        async with async_session_factory() as db:
            if workspace_id:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            if user_id:
                await db.execute(delete(PortalUser).where(PortalUser.id == user_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_onboarding_tick_refuses_a_showcase_workspace(monkeypatch):
    """The public Kaveri showcase and the 2015 ledger already have their own real history -- onboarding never
    writes sample invoices into either of them."""
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    workspace_id = uuid.uuid4()
    try:
        async with async_session_factory() as db:
            db.add(PortalWorkspace(id=workspace_id, name="Not an onboarding workspace", kind="showcase"))
            await db.commit()
            token = (await portal.issue(db, workspace_id, None))["token"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/portal/onboarding-tick", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 403
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            await db.commit()
        await engine.dispose()
