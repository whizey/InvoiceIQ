"""app/tools/email_provider.py — never a live Resend call in this suite
(conftest.py nulls resend_api_key/resend_from for every test)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.tools.email_provider import (
    ResendEmailProvider,
    SimulatedEmailProvider,
    get_email_provider,
)

def test_get_email_provider_is_simulated_when_unconfigured():
    assert isinstance(get_email_provider(), SimulatedEmailProvider)


def test_get_email_provider_is_resend_once_configured(monkeypatch):
    monkeypatch.setattr(settings, "resend_api_key", "re_fake_key")
    monkeypatch.setattr(settings, "resend_from", "demo@example.com")
    provider = get_email_provider()
    assert isinstance(provider, ResendEmailProvider)


@pytest.mark.asyncio
async def test_simulated_provider_never_makes_a_network_call():
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=AssertionError("must not be called"))):
        status = await SimulatedEmailProvider().send(to="x@example.com", subject="s", body="b")
    assert status == "FAILED"


@pytest.mark.asyncio
async def test_resend_provider_sends_with_correct_payload():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_post = AsyncMock(return_value=mock_response)

    provider = ResendEmailProvider(api_key="re_fake", from_address="demo@example.com")
    with patch("httpx.AsyncClient.post", new=mock_post):
        status = await provider.send(to="talepa.rahul6@gmail.com", subject="hello", body="world")

    assert status == "SENT"
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.resend.com/emails"
    assert kwargs["headers"]["Authorization"] == "Bearer re_fake"
    assert kwargs["json"]["to"] == ["talepa.rahul6@gmail.com"]
    assert kwargs["json"]["from"] == "demo@example.com"


@pytest.mark.asyncio
async def test_resend_provider_failure_returns_failed_not_raise():
    mock_post = AsyncMock(side_effect=RuntimeError("network down"))
    provider = ResendEmailProvider(api_key="re_fake", from_address="demo@example.com")
    with patch("httpx.AsyncClient.post", new=mock_post):
        status = await provider.send(to="talepa.rahul6@gmail.com", subject="s", body="b")
    assert status == "FAILED"
