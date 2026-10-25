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


def test_authorize_url_always_starts_at_the_global_accounts_domain(monkeypatch):
    """Every account authorizes through accounts.zoho.com regardless of its own data centre --
    only the later token exchange varies, so this never reads zoho_accounts_domain."""
    monkeypatch.setattr(settings, "zoho_client_id", "my-client-id")
    monkeypatch.setattr(settings, "zoho_accounts_domain", "https://accounts.zoho.in")
    url = zoho_sync.authorize_url("a-one-time-state")
    assert url.startswith("https://accounts.zoho.com/oauth/v2/auth?")
    assert "client_id=my-client-id" in url and "state=a-one-time-state" in url


@pytest.mark.asyncio
async def test_exchange_code_uses_the_accounts_server_zoho_reported_not_a_fixed_domain(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    monkeypatch.setattr(settings, "zoho_redirect_uri", "https://api.invalid/portal/zoho-callback")
    monkeypatch.setattr(settings, "zoho_accounts_domain", "https://accounts.zoho.com")  # the unused fallback
    mock_response = MagicMock(status_code=200, content=b"{}")
    mock_response.json.return_value = {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600,
                                       "api_domain": "https://www.zohoapis.in"}
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", new=mock_post):
        body = await zoho_sync.exchange_code("a-code", "https://accounts.zoho.in")
    assert body["access_token"] == "tok"
    assert mock_post.call_args[0][0] == "https://accounts.zoho.in/oauth/v2/token"


@pytest.mark.asyncio
async def test_exchange_code_falls_back_to_the_configured_domain_when_zoho_omits_it(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    monkeypatch.setattr(settings, "zoho_redirect_uri", "https://api.invalid/portal/zoho-callback")
    monkeypatch.setattr(settings, "zoho_accounts_domain", "https://accounts.zoho.com")
    mock_response = MagicMock(status_code=200, content=b"{}")
    mock_response.json.return_value = {"access_token": "tok"}
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", new=mock_post):
        await zoho_sync.exchange_code("a-code", None)
    assert mock_post.call_args[0][0] == "https://accounts.zoho.com/oauth/v2/token"


@pytest.mark.asyncio
async def test_exchange_code_raises_when_zoho_rejects_the_code(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    monkeypatch.setattr(settings, "zoho_redirect_uri", "https://api.invalid/portal/zoho-callback")
    mock_response = MagicMock(status_code=400, content=b'{"error": "invalid_code"}')
    mock_response.json.return_value = {"error": "invalid_code"}
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError, match="invalid_code"):
            await zoho_sync.exchange_code("a-bad-code", "https://accounts.zoho.in")


def zoho_invoice(**overrides):
    body = {"invoice_id": "4317942000000039021", "invoice_number": "INV-9", "customer_name": "Acme Co",
            "date": "2026-10-01", "due_date": "2026-10-31", "total": "10000.00", "balance": "4000.00",
            "status": "sent"}
    body.update(overrides)
    return {"invoice": body}


def test_map_invoice_reads_the_amount_paid_as_total_minus_balance():
    mapped = zoho_sync.map_invoice(zoho_invoice())
    assert mapped == {"invoice_number": "INV-9", "counterparty": "Acme Co", "issue_date": date(2026, 10, 1),
                      "due_date": date(2026, 10, 31), "amount_total": Decimal("10000.00"),
                      "amount_paid": Decimal("6000.00"), "zoho_invoice_id": "4317942000000039021"}


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


@pytest.mark.asyncio
async def test_refresh_access_token_uses_the_stored_accounts_server(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    mock_response = MagicMock(status_code=200, content=b"{}")
    mock_response.json.return_value = {"access_token": "new-tok", "expires_in": 3600}
    mock_post = AsyncMock(return_value=mock_response)
    with patch("httpx.AsyncClient.post", new=mock_post):
        body = await zoho_sync.refresh_access_token("a-refresh-token", "https://accounts.zoho.in")
    assert body["access_token"] == "new-tok"
    assert mock_post.call_args[0][0] == "https://accounts.zoho.in/oauth/v2/token"


@pytest.mark.asyncio
async def test_refresh_access_token_raises_when_zoho_rejects_it(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "id")
    monkeypatch.setattr(settings, "zoho_client_secret", "secret")
    mock_response = MagicMock(status_code=400, content=b'{"error": "invalid_refresh_token"}')
    mock_response.json.return_value = {"error": "invalid_refresh_token"}
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError, match="invalid_refresh_token"):
            await zoho_sync.refresh_access_token("a-stale-token", "https://accounts.zoho.in")


@pytest.mark.asyncio
async def test_list_organizations_returns_the_organizations_list():
    mock_response = MagicMock(status_code=200, content=b"{}")
    mock_response.json.return_value = {"organizations": [{"organization_id": "123", "is_default_org": True}]}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
        orgs = await zoho_sync.list_organizations("a-token", "https://www.zohoapis.in")
    assert orgs == [{"organization_id": "123", "is_default_org": True}]


@pytest.mark.asyncio
async def test_list_organizations_raises_on_failure():
    mock_response = MagicMock(status_code=401, content=b'{"message": "invalid token"}')
    mock_response.json.return_value = {"message": "invalid token"}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError, match="invalid token"):
            await zoho_sync.list_organizations("a-bad-token", "https://www.zohoapis.in")


@pytest.mark.asyncio
async def test_list_invoices_follows_pagination_until_the_last_page():
    page_one = MagicMock(status_code=200, content=b"{}")
    page_one.json.return_value = {"invoices": [{"invoice_number": "A"}],
                                  "page_context": {"page": 1, "has_more_page": True}}
    page_two = MagicMock(status_code=200, content=b"{}")
    page_two.json.return_value = {"invoices": [{"invoice_number": "B"}],
                                  "page_context": {"page": 2, "has_more_page": False}}
    mock_get = AsyncMock(side_effect=[page_one, page_two])
    with patch("httpx.AsyncClient.get", new=mock_get):
        invoices = await zoho_sync.list_invoices("a-token", "https://www.zohoapis.in", "123")
    assert [i["invoice_number"] for i in invoices] == ["A", "B"]
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[1].kwargs["params"]["page"] == 2


@pytest.mark.asyncio
async def test_list_invoices_stops_at_the_backfill_cap(monkeypatch):
    monkeypatch.setattr(zoho_sync, "MAX_BACKFILL_INVOICES", 3)
    page = MagicMock(status_code=200, content=b"{}")
    page.json.return_value = {"invoices": [{"invoice_number": str(i)} for i in range(200)],
                              "page_context": {"page": 1, "has_more_page": True}}
    mock_get = AsyncMock(return_value=page)
    with patch("httpx.AsyncClient.get", new=mock_get):
        invoices = await zoho_sync.list_invoices("a-token", "https://www.zohoapis.in", "123")
    assert len(invoices) == 3
    assert mock_get.call_count == 1  # the cap is hit within the first page; no second request


@pytest.mark.asyncio
async def test_list_invoices_raises_on_failure():
    mock_response = MagicMock(status_code=500, content=b'{"message": "server error"}')
    mock_response.json.return_value = {"message": "server error"}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError, match="server error"):
            await zoho_sync.list_invoices("a-token", "https://www.zohoapis.in", "123")


def zoho_payment(**overrides):
    body = {"payment_id": "900001", "date": "2026-10-20",
            "invoices": [{"invoice_id": "4317942000000039021", "amount_applied": "1000.00"}]}
    body.update(overrides)
    return {"payment": body}


def test_map_payment_reads_every_invoice_allocation():
    mapped = zoho_sync.map_payment(zoho_payment(invoices=[
        {"invoice_id": "A1", "amount_applied": "400.00"},
        {"invoice_id": "A2", "amount_applied": "600.00"},
    ]))
    assert mapped == {"zoho_payment_id": "900001", "paid_on": date(2026, 10, 20),
                      "allocations": [{"zoho_invoice_id": "A1", "amount": Decimal("400.00")},
                                     {"zoho_invoice_id": "A2", "amount": Decimal("600.00")}]}


def test_map_payment_drops_an_unusable_allocation_line_but_keeps_the_rest():
    mapped = zoho_sync.map_payment(zoho_payment(invoices=[
        {"invoice_id": "", "amount_applied": "400.00"},  # no invoice id: dropped
        {"invoice_id": "A2", "amount_applied": "0"},  # zero applied: dropped
        {"invoice_id": "A4", "amount_applied": "not-a-number"},  # unparseable: dropped
        {"invoice_id": "A3", "amount_applied": "600.00"},
    ]))
    assert mapped["allocations"] == [{"zoho_invoice_id": "A3", "amount": Decimal("600.00")}]


@pytest.mark.parametrize("overrides,reason", [
    ({"payment_id": ""}, "Missing payment id"),
    ({"date": "not-a-date"}, "payment date"),
    ({"invoices": []}, "not allocated"),
    ({"invoices": [{"invoice_id": "", "amount_applied": "0"}]}, "not allocated"),
])
def test_map_payment_rejects_an_unusable_payload(overrides, reason):
    with pytest.raises(ValueError, match=reason):
        zoho_sync.map_payment(zoho_payment(**overrides))


def test_map_payment_rejects_a_payload_with_no_payment_object():
    with pytest.raises(ValueError, match="no payment object"):
        zoho_sync.map_payment({"invoice": {}})


@pytest.mark.asyncio
async def test_list_payments_follows_pagination_until_the_last_page():
    page_one = MagicMock(status_code=200, content=b"{}")
    page_one.json.return_value = {"customer_payments": [{"payment_id": "1"}],
                                  "page_context": {"page": 1, "has_more_page": True}}
    page_two = MagicMock(status_code=200, content=b"{}")
    page_two.json.return_value = {"customer_payments": [{"payment_id": "2"}],
                                  "page_context": {"page": 2, "has_more_page": False}}
    mock_get = AsyncMock(side_effect=[page_one, page_two])
    with patch("httpx.AsyncClient.get", new=mock_get):
        payments = await zoho_sync.list_payments("a-token", "https://www.zohoapis.in", "123")
    assert [p["payment_id"] for p in payments] == ["1", "2"]
    assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_list_payments_raises_on_failure():
    mock_response = MagicMock(status_code=401, content=b'{"message": "invalid token"}')
    mock_response.json.return_value = {"message": "invalid token"}
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(ValueError, match="invalid token"):
            await zoho_sync.list_payments("a-bad-token", "https://www.zohoapis.in", "123")


def zoho_backfilled_payment(**overrides):
    body = {"payment_id": "900002", "invoice_number": "INV-1", "date": "2026-10-20", "amount": "500.00"}
    body.update(overrides)
    return body


def test_map_backfilled_payment_reads_the_single_invoice_allocation():
    mapped = zoho_sync.map_backfilled_payment(zoho_backfilled_payment())
    assert mapped == {"zoho_payment_id": "900002", "paid_on": date(2026, 10, 20),
                      "invoice_number": "INV-1", "amount": Decimal("500.00")}


@pytest.mark.parametrize("overrides,reason", [
    ({"payment_id": ""}, "Missing payment id"),
    ({"invoice_number": ""}, "Missing payment id"),
    ({"date": "not-a-date"}, "payment date"),
    ({"amount": "not-a-number"}, "not a number"),
    ({"amount": "0"}, "zero or negative"),
])
def test_map_backfilled_payment_rejects_an_unusable_row(overrides, reason):
    with pytest.raises(ValueError, match=reason):
        zoho_sync.map_backfilled_payment(zoho_backfilled_payment(**overrides))
