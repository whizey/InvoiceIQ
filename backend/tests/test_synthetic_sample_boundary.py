"""A sample flag alone must not publish an unmarked database or invoke providers."""

from unittest.mock import MagicMock

import httpx
import pytest

from app.agents.llm_client import FALLBACK_MODEL_NAME, _phase7_original_diagnose, voice_turn
from app.core.config import settings
from app.core.db import engine
from app.main import app
from scripts import initialize_synthetic_sample


@pytest.mark.asyncio
async def test_unmarked_database_fails_closed_in_synthetic_mode(monkeypatch):
    monkeypatch.setenv("SAMPLE_SYNTHETIC", "true")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/companies")
            assert response.status_code == 503
            assert response.json()["detail"] == "Synthetic sample provenance is unavailable"
            assert (await client.post("/invoices", json={})).status_code == 403
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_synthetic_mode_never_constructs_a_provider(monkeypatch):
    monkeypatch.setenv("SAMPLE_SYNTHETIC", "true")
    monkeypatch.setattr(settings, "google_api_key", "disposable-test-only-key")
    provider = MagicMock(side_effect=AssertionError("Provider must not be constructed"))
    monkeypatch.setattr("app.agents.llm_client._llm_chat", provider)
    invoice = {"days_overdue": 5, "amount_total": 2000}
    customer = {"num_prior_late_payments": 0, "num_prior_on_time_payments": 1}
    result, model = await _phase7_original_diagnose(invoice, customer)
    assert result.recommended_priority == "low"
    assert model == FALLBACK_MODEL_NAME
    voice, voice_model = await voice_turn(invoice, customer, [], 1)
    assert voice_model == FALLBACK_MODEL_NAME
    assert voice.agent_line_hi
    provider.assert_not_called()


def test_sample_initializer_rejects_non_sample_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:dummy@localhost/invoiceiq_development")
    monkeypatch.setenv("SAMPLE_SYNTHETIC", "true")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")
    with pytest.raises(RuntimeError, match="dedicated sample names"):
        initialize_synthetic_sample.main()


def test_sample_initializer_rejects_existing_unmarked_schema(monkeypatch):
    async def unmarked():
        return "unmarked"

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:dummy@localhost/invoiceiq_sample")
    monkeypatch.setenv("SAMPLE_SYNTHETIC", "true")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")
    monkeypatch.setattr(initialize_synthetic_sample, "database_state", unmarked)
    monkeypatch.setattr(initialize_synthetic_sample.subprocess, "run", MagicMock(side_effect=AssertionError("No database writes")))
    with pytest.raises(RuntimeError, match="lacks verified synthetic provenance"):
        initialize_synthetic_sample.main()
