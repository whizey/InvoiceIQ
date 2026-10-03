from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.llm_client import FALLBACK_MODEL_NAME, GEMINI_DEFAULT_MODEL, diagnose, recommend, voice_turn
from app.agents.schemas import DiagnosisResult, InterventionRecommendation
from app.core.config import settings

pytestmark = pytest.mark.asyncio(loop_scope="module")

_INVOICE = {"days_overdue": 5, "amount_total": 150000}
_CUSTOMER = {
    "num_prior_late_payments": 0,
    "num_prior_on_time_payments": 3,
}


async def test_no_keys_uses_rule_based_fallback(caplog):
    caplog.set_level("INFO", logger="app.llm")
    diagnosis, model = await diagnose(_INVOICE, _CUSTOMER)
    assert model == FALLBACK_MODEL_NAME
    assert diagnosis.recommended_priority == "low"

    rec, rec_model = await recommend(_INVOICE, _CUSTOMER, diagnosis.model_dump(), reminder_count=0)
    assert rec_model == FALLBACK_MODEL_NAME
    assert rec.action == "SEND_EMAIL"
    messages = [r.getMessage() for r in caplog.records]
    assert any("called=False" in m and "diagnose" in m for m in messages)


async def test_google_key_uses_gemini_default_model(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", "test-google-key")
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(settings, "llm_model", None)

    structured = MagicMock()
    structured.ainvoke = AsyncMock(
        return_value=DiagnosisResult(
            diagnosis="Gemini diagnosis",
            reason="mocked",
            recommended_priority="medium",
        )
    )
    chat = MagicMock()
    chat.with_structured_output.return_value = structured
    factory = MagicMock(return_value=chat)

    import langchain_google_genai

    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", factory)

    result, model = await diagnose(_INVOICE, _CUSTOMER)
    assert model == GEMINI_DEFAULT_MODEL
    assert result.diagnosis == "Gemini diagnosis"
    factory.assert_called_once()
    assert factory.call_args.kwargs["model"] == GEMINI_DEFAULT_MODEL
    assert factory.call_args.kwargs["api_key"] == "test-google-key"


async def test_openai_used_when_google_key_absent(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")
    monkeypatch.setattr(settings, "llm_model", None)

    structured = MagicMock()
    structured.ainvoke = AsyncMock(
        return_value=InterventionRecommendation(
            action="WAIT",
            rationale="mocked",
        )
    )
    chat = MagicMock()
    chat.with_structured_output.return_value = structured
    factory = MagicMock(return_value=chat)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", factory)

    rec, model = await recommend(_INVOICE, _CUSTOMER, {"diagnosis": "x"}, reminder_count=0)
    assert model == "gpt-4o-mini"
    assert rec.action == "WAIT"
    factory.assert_called_once()
    assert factory.call_args.kwargs["model"] == "gpt-4o-mini"


async def test_configured_llm_error_falls_back_to_rule_based_not_raise(monkeypatch, caplog):
    """The real bug this guards against: a configured Gemini/OpenAI key that
    starts failing (quota exhausted, network error, timeout) must never
    surface as an unhandled exception to the caller — it falls back to the
    same deterministic result a missing key would produce."""
    monkeypatch.setattr(settings, "google_api_key", "test-google-key")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    caplog.set_level("WARNING", logger="app.llm")

    structured = MagicMock()
    structured.ainvoke = AsyncMock(side_effect=RuntimeError("429 RESOURCE_EXHAUSTED"))
    chat = MagicMock()
    chat.with_structured_output.return_value = structured
    factory = MagicMock(return_value=chat)

    import langchain_google_genai

    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", factory)

    diagnosis, model = await diagnose(_INVOICE, _CUSTOMER)
    assert model == FALLBACK_MODEL_NAME
    assert diagnosis.recommended_priority == "low"  # same result the no-key fallback produces
    assert any("falling back to rule-based" in r.getMessage() for r in caplog.records)


async def test_voice_turn_falls_back_when_configured_llm_errors(monkeypatch):
    """The exact bug hit live: /recovery-cases/{id}/voice/start returning a
    500 because the configured Gemini key had exhausted its quota. voice_turn
    must fall back instead of propagating the error."""
    monkeypatch.setattr(settings, "google_api_key", "test-google-key")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)

    structured = MagicMock()
    structured.ainvoke = AsyncMock(side_effect=RuntimeError("429 RESOURCE_EXHAUSTED"))
    chat = MagicMock()
    chat.with_structured_output.return_value = structured
    factory = MagicMock(return_value=chat)

    import langchain_google_genai

    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", factory)

    result, model = await voice_turn(_INVOICE, _CUSTOMER, [], turn_number=1)
    assert model == FALLBACK_MODEL_NAME
    assert result.proposed_action == "NONE"
    assert result.concluded is False
