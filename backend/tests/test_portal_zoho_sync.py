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

_real_get = AsyncClient.get


def zoho_only_get(zoho_response):
    """A GET patch answering only calls to Zoho's own domains with `zoho_response`; a call to our own
    testserver (the test client's own request, since it is itself an AsyncClient) passes through to the real
    implementation -- patching AsyncClient.get unconditionally would intercept both indistinguishably. The
    test client calls with a path relative to its base_url ("/portal/..."); only an internal call to Zoho
    uses an absolute URL, so that's what distinguishes the two here."""
    async def fake_get(self, url, *args, **kwargs):
        if not str(url).startswith("http"):
            return await _real_get(self, url, *args, **kwargs)
        return zoho_response
    return fake_get


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


async def connected(client, headers, workspace_id) -> str:
    """Connect and complete the OAuth callback; returns the resulting webhook secret."""
    await client.post("/portal/zoho-connect", headers=headers)
    async with async_session_factory() as db:
        state = (await db.scalar(select(PortalZohoConnection).where(
            PortalZohoConnection.workspace_id == workspace_id))).oauth_state
    token_response = MagicMock(status_code=200, content=b"{}")
    token_response.json.return_value = {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600,
                                        "api_domain": "https://www.zohoapis.in"}
    orgs_response = MagicMock(status_code=200, content=b"{}")
    orgs_response.json.return_value = {"organizations": [{"organization_id": "999", "is_default_org": True}]}
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=token_response)), \
         patch("httpx.AsyncClient.get", new=zoho_only_get(orgs_response)):
        callback = await client.get("/portal/zoho-callback", params={
            "code": "c", "state": state, "accounts-server": "https://accounts.zoho.in"})
    assert callback.status_code in (302, 307)
    status = await client.get("/portal/zoho-status", headers=headers)
    return status.json()["webhook_url"].rsplit("/", 1)[1]


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
            orgs_response = MagicMock(status_code=200, content=b"{}")
            orgs_response.json.return_value = {"organizations": [{"organization_id": "999",
                                                                   "is_default_org": True}]}
            with patch("httpx.AsyncClient.post", new=mock_post), \
                 patch("httpx.AsyncClient.get", new=zoho_only_get(orgs_response)):
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


@pytest.mark.asyncio
async def test_sync_now_pulls_invoices_that_predate_the_webhook(monkeypatch):
    """A webhook only fires on a future create or update -- Sync Now is the only way invoices that already
    existed in Zoho before connecting ever reach InvoiceIQ."""
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    configure(monkeypatch)
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Backfill Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)

            await client.post("/portal/zoho-connect", headers=headers)
            async with async_session_factory() as db:
                state = (await db.scalar(select(PortalZohoConnection).where(
                    PortalZohoConnection.workspace_id == workspace_id))).oauth_state
            token_response = MagicMock(status_code=200, content=b"{}")
            token_response.json.return_value = {"access_token": "tok", "refresh_token": "ref",
                                                "expires_in": 3600, "api_domain": "https://www.zohoapis.in"}
            orgs_response = MagicMock(status_code=200, content=b"{}")
            orgs_response.json.return_value = {"organizations": [{"organization_id": "999",
                                                                   "is_default_org": True}]}
            with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=token_response)), \
                 patch("httpx.AsyncClient.get", new=zoho_only_get(orgs_response)):
                callback = await client.get("/portal/zoho-callback", params={
                    "code": "c", "state": state, "accounts-server": "https://accounts.zoho.in"})
            assert callback.status_code in (302, 307)

            invoices_response = MagicMock(status_code=200, content=b"{}")
            invoices_response.json.return_value = {"invoices": [
                {"invoice_number": "OLD-1", "customer_name": "Pre-existing Co", "date": "2026-09-01",
                 "due_date": "2026-09-30", "total": "2000.00", "balance": "2000.00", "status": "sent"},
                {"invoice_number": "OLD-2", "customer_name": "Draft Co", "date": "2026-09-05",
                 "due_date": "2026-10-05", "total": "500.00", "balance": "500.00", "status": "draft"},
            ], "page_context": {"page": 1, "has_more_page": False}}
            with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=invoices_response)):
                sync = await client.post("/portal/zoho-sync-now", headers=headers)
            assert sync.status_code == 200
            body = sync.json()
            # The mock answers every GET the same way, so the payments-list call (no "customer_payments" key
            # in that body) sees an empty list rather than erroring -- harmless, not what this test covers.
            assert body == {"ok": True, "found": 2, "imported": 1, "skipped": 1, "scored": body["scored"],
                            "payments_found": 0, "payments_applied": 0, "payments_skipped": 0,
                            "payments_error": None}

            invoices = await client.get("/portal/invoices?q=OLD-1", headers=headers)
            assert invoices.json()["total"] == 1
            assert invoices.json()["invoices"][0]["amount"] == "2000.00"
            assert (await client.get("/portal/invoices?q=OLD-2", headers=headers)).json()["total"] == 0
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
async def test_sync_now_refuses_without_a_connection(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho No Connection Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            response = await client.post("/portal/zoho-sync-now", headers=headers)
            assert response.status_code == 409
    finally:
        async with async_session_factory() as db:
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sync_now_refuses_cleanly_for_a_connection_missing_api_domain(monkeypatch):
    """A connection made before data-centre auto-detection shipped never got api_domain populated -- Sync
    Now must refuse with a clear error, not crash with AttributeError on a None domain (as it did live)."""
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    configure(monkeypatch)
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Legacy Connection Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            await connected(client, headers, workspace_id)
            async with async_session_factory() as db:
                connection = await db.scalar(select(PortalZohoConnection).where(
                    PortalZohoConnection.workspace_id == workspace_id))
                connection.api_domain = None  # simulate a pre-auto-detection connection
                await db.commit()

            response = await client.post("/portal/zoho-sync-now", headers=headers)
            assert response.status_code == 409
            assert "reconnect" in response.json()["detail"].lower()
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
async def test_payment_webhook_reconciles_a_real_payment_against_its_invoice(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    configure(monkeypatch)
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Payment Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            secret = await connected(client, headers, workspace_id)

            created = await client.post(f"/portal/zoho-webhook/{secret}", json={"invoice": {
                "invoice_id": "ZINV-1", "invoice_number": "PAY-1", "customer_name": "Payment Test Co",
                "date": "2026-10-01", "due_date": "2026-10-31", "total": "1000.00", "balance": "1000.00",
                "status": "sent"}})
            assert created.status_code == 200 and created.json()["created"] is True

            payment = await client.post(f"/portal/zoho-webhook/{secret}", json={"payment": {
                "payment_id": "ZPAY-1", "date": "2026-10-15",
                "invoices": [{"invoice_id": "ZINV-1", "amount_applied": "400.00"}]}})
            assert payment.status_code == 200
            body = payment.json()
            assert body["ok"] is True and body["applied"] == 1 and body["unmatched"] == 0

            row = (await client.get("/portal/invoices?q=PAY-1", headers=headers)).json()["invoices"][0]
            assert row["paid"] == "400.00" and row["balance"] == "600.00"

            # Re-delivering the same payment (a Zoho retry, or the user re-saving it) updates, not duplicates.
            again = await client.post(f"/portal/zoho-webhook/{secret}", json={"payment": {
                "payment_id": "ZPAY-1", "date": "2026-10-16",
                "invoices": [{"invoice_id": "ZINV-1", "amount_applied": "400.00"}]}})
            assert again.status_code == 200 and again.json()["applied"] == 1
            row_again = (await client.get("/portal/invoices?q=PAY-1", headers=headers)).json()["invoices"][0]
            assert row_again["paid"] == "400.00"  # not 800 -- the second delivery updated the one row

            # The old balance-delta fallback does not also invent a second payment now a real one exists.
            invoice_update = await client.post(f"/portal/zoho-webhook/{secret}", json={"invoice": {
                "invoice_id": "ZINV-1", "invoice_number": "PAY-1", "customer_name": "Payment Test Co",
                "date": "2026-10-01", "due_date": "2026-10-31", "total": "1000.00", "balance": "600.00",
                "status": "sent"}})
            assert invoice_update.status_code == 200

            events = (await client.get("/portal/zoho-status", headers=headers)).json()["events"]
            payment_events = [e for e in events if e["outcome"] == "payment"]
            assert len(payment_events) == 2 and payment_events[0]["invoice_number"] == "PAY-1"

            # A payment allocated against an invoice we've never synced is reported unmatched, not an error.
            orphan = await client.post(f"/portal/zoho-webhook/{secret}", json={"payment": {
                "payment_id": "ZPAY-2", "date": "2026-10-16",
                "invoices": [{"invoice_id": "UNKNOWN", "amount_applied": "50.00"}]}})
            assert orphan.status_code == 200
            assert orphan.json() == {"ok": True, "applied": 0, "unmatched": 1, "scored": 0}

            # A payment payload that can't be parsed is skipped, not a server error.
            bad = await client.post(f"/portal/zoho-webhook/{secret}", json={"payment": {"payment_id": ""}})
            assert bad.status_code == 200 and bad.json()["ok"] is False
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
async def test_sync_now_also_backfills_payments(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    configure(monkeypatch)
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Payment Backfill Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            await connected(client, headers, workspace_id)

            invoices_page = MagicMock(status_code=200, content=b"{}")
            invoices_page.json.return_value = {"invoices": [
                {"invoice_number": "BF-1", "customer_name": "Backfill Co", "date": "2026-09-01",
                 "due_date": "2026-09-30", "total": "2000.00", "balance": "2000.00", "status": "sent"},
            ], "page_context": {"page": 1, "has_more_page": False}}
            payments_page = MagicMock(status_code=200, content=b"{}")
            payments_page.json.return_value = {"customer_payments": [
                {"payment_id": "BFPAY-1", "invoice_number": "BF-1", "date": "2026-09-15", "amount": "800.00"},
                {"payment_id": "", "invoice_number": "BF-1", "date": "2026-09-16", "amount": "100.00"},  # unusable
            ], "page_context": {"page": 1, "has_more_page": False}}
            with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=[invoices_page, payments_page])):
                sync = await client.post("/portal/zoho-sync-now", headers=headers)
            assert sync.status_code == 200
            body = sync.json()
            assert body["imported"] == 1
            assert body["payments_found"] == 2 and body["payments_applied"] == 1 and body["payments_skipped"] == 1
            assert body["payments_error"] is None

            row = (await client.get("/portal/invoices?q=BF-1", headers=headers)).json()["invoices"][0]
            assert row["paid"] == "800.00" and row["balance"] == "1200.00"

            # Running Sync Now again with the same payment (a different amount, to make a real change
            # observable) updates the existing backfilled row rather than adding a second one.
            payments_page_again = MagicMock(status_code=200, content=b"{}")
            payments_page_again.json.return_value = {"customer_payments": [
                {"payment_id": "BFPAY-1", "invoice_number": "BF-1", "date": "2026-09-17", "amount": "900.00"},
            ], "page_context": {"page": 1, "has_more_page": False}}
            with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=[invoices_page, payments_page_again])):
                sync_again = await client.post("/portal/zoho-sync-now", headers=headers)
            assert sync_again.json()["payments_applied"] == 1
            row_again = (await client.get("/portal/invoices?q=BF-1", headers=headers)).json()["invoices"][0]
            assert row_again["paid"] == "900.00"  # replaced, not 800 + 900
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
async def test_sync_now_reports_a_payments_failure_without_discarding_the_invoice_import(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    configure(monkeypatch)
    await engine.dispose()
    tag = uuid.uuid4().hex
    workspace_ids, user_ids = [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            headers, workspace_id, user_id = await register(client, tag, "Zoho Payment Failure Co")
            workspace_ids.append(workspace_id)
            user_ids.append(user_id)
            await connected(client, headers, workspace_id)

            invoices_page = MagicMock(status_code=200, content=b"{}")
            invoices_page.json.return_value = {"invoices": [
                {"invoice_number": "FAIL-1", "customer_name": "Failure Co", "date": "2026-09-01",
                 "due_date": "2026-09-30", "total": "500.00", "balance": "500.00", "status": "sent"},
            ], "page_context": {"page": 1, "has_more_page": False}}
            payments_failure = MagicMock(status_code=500, content=b'{"message": "server error"}')
            payments_failure.json.return_value = {"message": "server error"}
            with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=[invoices_page, payments_failure])):
                sync = await client.post("/portal/zoho-sync-now", headers=headers)
            assert sync.status_code == 200
            body = sync.json()
            assert body["imported"] == 1  # the invoice import still succeeded and committed
            assert body["payments_error"] == "server error"

            assert (await client.get("/portal/invoices?q=FAIL-1", headers=headers)).json()["total"] == 1
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalZohoConnection).where(PortalZohoConnection.workspace_id.in_(workspace_ids)))
            if workspace_ids:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspace_ids)))
            if user_ids:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(user_ids)))
            await db.commit()
        await engine.dispose()
