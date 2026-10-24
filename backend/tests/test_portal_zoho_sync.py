"""The Zoho integration endpoints: connect issues a one-time state, the callback redeems it, and the webhook
upserts invoices by the secret in its own URL (Zoho's classic webhooks carry no signature to check instead)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.main import app
from app.models.portal import PortalUser, PortalWorkspace, PortalZohoConnection


async def register(client, tag, name):
    reg = await client.post("/portal/register", json={
        "email": f"owner-{tag}@example.invalid", "full_name": "Test Owner",
        "company": name, "password": "A-strong-unique-long-password-37"})
    assert reg.status_code == 201, reg.text
    headers = {"Authorization": f"Bearer {reg.json()['token']}"}
    me = (await client.get("/portal/me", headers=headers)).json()
    return headers, uuid.UUID(me["workspace_id"]), uuid.UUID(me["user_id"])


def configure(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "test-client-id")
    monkeypatch.setattr(settings, "zoho_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "zoho_redirect_uri", "https://api.invalid/portal/zoho-callback")
    monkeypatch.setattr(settings, "frontend_public_url", "https://site.invalid")


@pytest.mark.asyncio
async def test_status_reports_unconfigured_and_disconnected_by_default(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Status Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            status = await client.get("/portal/zoho-status", headers=headers)
            assert status.status_code == 200
            assert status.json() == {"configured": False, "connected": False, "connected_at": None,
                                     "last_synced_at": None, "invoices_synced": 0, "webhook_url": None,
                                     "api_domain": None, "events": []}
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalZohoConnection).where(PortalZohoConnection.workspace_id.in_(workspace_ids)))
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_connect_refuses_when_zoho_is_not_configured(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Unconfigured Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            response = await client.post("/portal/zoho-connect", headers=headers)
            assert response.status_code == 503
    finally:
        async with async_session_factory() as db:
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_connect_then_callback_then_webhook_round_trip(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    configure(monkeypatch)
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Round Trip Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)

            connect = await client.post("/portal/zoho-connect", headers=headers)
            assert connect.status_code == 200
            assert connect.json()["authorize_url"].startswith("https://accounts.zoho.com/oauth/v2/auth?")

            async with async_session_factory() as db:
                connection = await db.scalar(select(PortalZohoConnection).where(
                    PortalZohoConnection.workspace_id == workspace_id))
                state = connection.oauth_state
            assert state

            # A wrong state does not redeem the pending connection.
            bad = await client.get("/portal/zoho-callback", params={"code": "c", "state": "wrong"})
            assert bad.status_code in (302, 307) and "zoho=expired" in bad.headers["location"]

            mock_response = MagicMock(status_code=200, content=b"{}")
            mock_response.json.return_value = {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600,
                                               "api_domain": "https://www.zohoapis.in"}
            mock_post = AsyncMock(return_value=mock_response)
            with patch("httpx.AsyncClient.post", new=mock_post):
                # accounts-server is Zoho's own report of which data centre issued the code -- an
                # account on any data centre must work, not just the one zoho_accounts_domain defaults to.
                callback = await client.get("/portal/zoho-callback", params={
                    "code": "a-real-code", "state": state, "accounts-server": "https://accounts.zoho.in"})
            assert callback.status_code in (302, 307)
            assert callback.headers["location"] == "https://site.invalid/settings?zoho=connected"
            assert mock_post.call_args[0][0] == "https://accounts.zoho.in/oauth/v2/token"

            status = await client.get("/portal/zoho-status", headers=headers)
            body = status.json()
            assert body["connected"] is True and body["connected_at"] is not None
            assert body["api_domain"] == "https://www.zohoapis.in"
            assert body["webhook_url"].endswith(f"/portal/zoho-webhook/{body['webhook_url'].rsplit('/', 1)[1]}")
            secret = body["webhook_url"].rsplit("/", 1)[1]

            # Re-authorizing without disconnecting first (a second Connect click, or a token refresh)
            # must not rotate the secret -- that would silently break a webhook URL already saved in Zoho.
            await client.post("/portal/zoho-connect", headers=headers)
            async with async_session_factory() as db:
                state2 = (await db.scalar(select(PortalZohoConnection).where(
                    PortalZohoConnection.workspace_id == workspace_id))).oauth_state
            with patch("httpx.AsyncClient.post", new=mock_post):
                await client.get("/portal/zoho-callback", params={
                    "code": "another-code", "state": state2, "accounts-server": "https://accounts.zoho.in"})
            status_again = await client.get("/portal/zoho-status", headers=headers)
            assert status_again.json()["webhook_url"].rsplit("/", 1)[1] == secret

            # An unknown secret is refused outright.
            assert (await client.post("/portal/zoho-webhook/not-the-real-secret", json={"invoice": {}})
                   ).status_code == 404

            created = await client.post(f"/portal/zoho-webhook/{secret}", json={"invoice": {
                "invoice_number": "ZB-1", "customer_name": "Streamed Co", "date": "2026-10-01",
                "due_date": "2026-10-31", "total": "5000.00", "balance": "5000.00", "status": "sent"}})
            assert created.status_code == 200 and created.json()["created"] is True

            invoices = await client.get("/portal/invoices?q=ZB-1", headers=headers)
            row = invoices.json()["invoices"][0]
            assert row["amount"] == "5000.00" and row["paid"] == "0.00"

            # The same invoice number, now marked paid: an update, not a second invoice, and a payment is logged.
            paid = await client.post(f"/portal/zoho-webhook/{secret}", json={"invoice": {
                "invoice_number": "ZB-1", "customer_name": "Streamed Co", "date": "2026-10-01",
                "due_date": "2026-10-31", "total": "5000.00", "balance": "0.00", "status": "sent"}})
            assert paid.status_code == 200 and paid.json()["created"] is False and paid.json()["paid"] is True

            invoices_after = await client.get("/portal/invoices?q=ZB-1", headers=headers)
            assert invoices_after.json()["total"] == 1
            assert invoices_after.json()["invoices"][0]["paid"] == "5000.00"

            # A draft invoice is skipped, not imported -- and that's visible, not silent.
            skipped = await client.post(f"/portal/zoho-webhook/{secret}", json={"invoice": {
                "invoice_number": "ZB-2", "customer_name": "Draft Co", "date": "2026-10-01",
                "due_date": "2026-10-31", "total": "900.00", "balance": "900.00", "status": "draft"}})
            assert skipped.status_code == 200 and skipped.json()["ok"] is False

            events = (await client.get("/portal/zoho-status", headers=headers)).json()["events"]
            assert [(e["invoice_number"], e["outcome"]) for e in events] == [
                ("ZB-2", "skipped"), ("ZB-1", "updated"), ("ZB-1", "created")]
            assert "draft" in events[0]["reason"]

            disconnect = await client.post("/portal/zoho-disconnect", headers=headers)
            assert disconnect.status_code == 200
            assert (await client.post(f"/portal/zoho-webhook/{secret}", json={"invoice": {}})).status_code == 404
            assert (await client.get("/portal/zoho-status", headers=headers)).json()["connected"] is False
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalZohoConnection).where(PortalZohoConnection.workspace_id.in_(workspace_ids)))
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_webhook_url_uses_the_public_https_scheme_not_the_internal_proxy_hop(monkeypatch):
    """uvicorn sees the Space's own internal (plain HTTP) hop to the app, not the real public HTTPS
    request -- the webhook URL has to come from X-Forwarded-Proto, or Zoho gets handed an http:// URL."""
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    configure(monkeypatch)
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Scheme Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            await client.post("/portal/zoho-connect", headers=headers)
            async with async_session_factory() as db:
                state = (await db.scalar(select(PortalZohoConnection).where(
                    PortalZohoConnection.workspace_id == workspace_id))).oauth_state
            mock_response = MagicMock(status_code=200, content=b"{}")
            mock_response.json.return_value = {"access_token": "tok"}
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
                await client.get("/portal/zoho-callback", params={"code": "c", "state": state})
            status = await client.get("/portal/zoho-status", headers={**headers, "X-Forwarded-Proto": "https"})
            assert status.json()["webhook_url"].startswith("https://")
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalZohoConnection).where(PortalZohoConnection.workspace_id.in_(workspace_ids)))
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()
