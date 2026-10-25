"""POST /portal/demo-start and /portal/demo-tick: a visitor's own isolated, ephemeral workspace that plays
through Kaveri's deterministic story one event at a time, through the real pipeline, not replayed from data
that already existed."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.api import portal
from app.core.db import async_session_factory, engine
from app.main import app
from app.models.portal import PortalDemoSession, PortalWorkspace


@pytest.mark.asyncio
async def test_demo_start_refuses_when_showcase_is_disabled(monkeypatch):
    monkeypatch.delenv("INVOICEIQ_PORTAL_SHOWCASE", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/portal/demo-start")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_demo_start_then_tick_writes_real_invoices_through_the_real_pipeline(monkeypatch):
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "true")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    workspace_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            start = await client.post("/portal/demo-start")
            assert start.status_code == 200
            body = start.json()
            assert body["kind"] == "showcase" and body["token"].startswith("iqp_")
            headers = {"Authorization": f"Bearer {body['token']}"}
            workspace_id = uuid.UUID((await client.get("/portal/me", headers=headers)).json()["workspace_id"])

            empty = await client.get("/portal/invoices", headers=headers)
            assert empty.json()["total"] == 0  # nothing written until the first tick

            tick = await client.post("/portal/demo-tick", headers=headers)
            assert tick.status_code == 200
            tick_body = tick.json()
            assert tick_body["cursor"] == 1 and len(tick_body["events"]) == 1
            first = tick_body["events"][0]
            assert first["kind"] == "invoice" and first["invoice"].startswith("KIS-")

            invoices = await client.get("/portal/invoices", headers=headers)
            assert invoices.json()["total"] == 1
            assert invoices.json()["invoices"][0]["number"] == first["invoice"]

            # A second tick with a larger batch advances further without re-writing the first event.
            tick2 = await client.post("/portal/demo-tick", params={"count": 5}, headers=headers)
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
                await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_demo_tick_refuses_a_non_demo_showcase_workspace(monkeypatch):
    """Confirms the identity() relaxation only ever accepts a genuine demo session's own workspace -- an
    arbitrary showcase-kind workspace with no PortalDemoSession row stays rejected, same as before."""
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    workspace_id = uuid.uuid4()
    try:
        async with async_session_factory() as db:
            db.add(PortalWorkspace(id=workspace_id, name="Not a demo session", kind="showcase"))
            await db.commit()
            token = (await portal.issue(db, workspace_id, None))["token"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/portal/demo-tick", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 403
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_demo_start_sweeps_sessions_old_enough_that_nobody_is_watching(monkeypatch):
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "true")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    stale_workspace_id = uuid.uuid4()
    try:
        async with async_session_factory() as db:
            db.add(PortalWorkspace(id=stale_workspace_id, name="Stale live demo", kind="showcase"))
            await db.flush()
            session = PortalDemoSession(workspace_id=stale_workspace_id)
            db.add(session)
            await db.flush()
            # Backdate it past the TTL directly, since created_at has a server-generated default.
            await db.execute(PortalDemoSession.__table__.update()
                             .where(PortalDemoSession.workspace_id == stale_workspace_id)
                             .values(created_at=datetime.now(timezone.utc) - timedelta(hours=3)))
            await db.commit()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/portal/demo-start")
            assert response.status_code == 200

        async with async_session_factory() as db:
            assert await db.get(PortalWorkspace, stale_workspace_id) is None
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == stale_workspace_id))
            await db.commit()
        await engine.dispose()
