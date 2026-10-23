"""app/services/zoho_sync.py — OAuth URLs, the token exchange, and mapping a webhook payload.

Never a live Zoho call in this suite (conftest.py nulls zoho_client_id/secret/redirect_uri for every test)."""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import zoho_sync


def test_not_configured_until_client_id_secret_and_redirect_uri_are_all_set(monkeypatch):
    assert zoho_sync.configured() is False
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    assert zoho_sync.configured() is False  # redirect_uri still unset
    monkeypatch.setattr(settings, "zoho_redirect_uri", "https://api.invalid/portal/zoho-callback")
    assert zoho_sync.configured() is True


def test_authorize_url_carries_the_state_and_client_id(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "my-client-id")
    monkeypatch.setattr(settings, "zoho_accounts_domain", "https://accounts.zoho.in")
    url = zoho_sync.authorize_url("a-one-time-state")
    assert url.startswith("https://accounts.zoho.in/oauth/v2/auth?")
    assert "client_id=my-client-id" in url and "state=a-one-time-state" in url


@pytest.mark.asyncio
async def test_exchange_code_returns_the_token_body_on_success(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    monkeypatch.setattr(settings, "zoho_redirect_uri", "https://api.invalid/portal/zoho-callback")
    mock_response = MagicMock(status_code=200, content=b"{}")
    mock_response.json.return_value = {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        body = await zoho_sync.exchange_code("a-code")
    assert body["access_token"] == "tok"


@pytest.mark.asyncio
async def test_exchange_code_raises_when_zoho_rejects_the_code(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    monkeypatch.setattr(settings, "zoho_redirect_uri", "https://api.invalid/portal/zoho-callback")
    mock_response = MagicMock(status_code=400, content=b'{"error": "invalid_code"}')
    mock_response.json.return_value = {"error": "invalid_code"}
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError, match="invalid_code"):
            await zoho_sync.exchange_code("a-bad-code")


def zoho_invoice(**overrides):
    body = {"invoice_number": "INV-9", "customer_name": "Acme Co", "date": "2026-10-01",
            "due_date": "2026-10-31", "total": "10000.00", "balance": "4000.00", "status": "sent"}
    body.update(overrides)
    return {"invoice": body}


def test_map_invoice_reads_the_amount_paid_as_total_minus_balance():
    mapped = zoho_sync.map_invoice(zoho_invoice())
    assert mapped == {"invoice_number": "INV-9", "counterparty": "Acme Co", "issue_date": date(2026, 10, 1),
                      "due_date": date(2026, 10, 31), "amount_total": Decimal("10000.00"),
                      "amount_paid": Decimal("6000.00")}


def test_map_invoice_defaults_the_due_date_when_zoho_omits_it():
    mapped = zoho_sync.map_invoice(zoho_invoice(due_date=None))
    assert mapped["due_date"] == date(2026, 10, 31)  # 30 days after the invoice date


@pytest.mark.parametrize("overrides,reason", [
    ({"status": "void"}, "status"),
    ({"status": "draft"}, "status"),
    ({"invoice_number": ""}, "Missing invoice number"),
    ({"customer_name": ""}, "Missing invoice number"),
    ({"date": "not-a-date"}, "invoice date"),
    ({"total": "not-a-number"}, "not a number"),
    ({"total": "0"}, "zero or negative"),
    ({"balance": "99999.00"}, "inconsistent"),
])
def test_map_invoice_rejects_an_unusable_payload(overrides, reason):
    with pytest.raises(ValueError, match=reason):
        zoho_sync.map_invoice(zoho_invoice(**overrides))


def test_map_invoice_rejects_a_payload_with_no_invoice_object():
    with pytest.raises(ValueError, match="no invoice object"):
        zoho_sync.map_invoice({"not_an_invoice": True})
