from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.llm_client import FALLBACK_MODEL_NAME, GEMINI_DEFAULT_MODEL, diagnose, recommend, voice_turn
from app.agents.schemas import DiagnosisResult, InterventionRecommendation
from app.core.config import settings


def test_groq_uses_openai_compatible_client_without_external_calls(monkeypatch):
    from app.agents.llm_client import _llm_chat, configured_llm
    import langchain_openai

    monkeypatch.setattr(settings, "google_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", "fake-groq-key")
    monkeypatch.setattr(settings, "groq_model", "explicit-test-model")
    factory = MagicMock()
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", factory)
    assert configured_llm() == ("groq", "explicit-test-model")
    _llm_chat()
    assert factory.call_args.kwargs["base_url"] == "https://api.groq.com/openai/v1"
    assert factory.call_args.kwargs["max_retries"] == 0


_INVOICE = {"days_overdue": 5, "amount_total": 150000}
_CUSTOMER = {
    "num_prior_late_payments": 0,
    "num_prior_on_time_payments": 3,
}


@pytest.mark.asyncio(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
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


@pytest.mark.asyncio(loop_scope="module")
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


@pytest.mark.parametrize("raw,expected", [
    (None, ["gemini-3.5-flash-lite"]),
    ("gemini-3.5-flash, models/gemini-3.5-flash-lite,,gemini-3.5-flash", ["gemini-3.5-flash", "gemini-3.5-flash-lite"]),
    ("gemini-3.6-flash,gemini-3.5-flash", ["gemini-3.5-flash"]),  # the primary model is never its own fallback
    ("none", []),
])
def test_gemini_fallback_models_are_ordered_and_deduplicated(monkeypatch, raw, expected):
    from app.agents.llm_client import gemini_fallback_chats, gemini_fallback_models

    monkeypatch.setattr(settings, "google_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "gemini-3.6-flash")
    monkeypatch.setattr(settings, "gemini_fallback_models", raw)
    assert gemini_fallback_models() == expected
    assert [model for _, model in gemini_fallback_chats()] == expected


def test_no_gemini_fallbacks_without_gemini(monkeypatch):
    from app.agents.llm_client import gemini_fallback_models

    monkeypatch.setattr(settings, "gemini_fallback_models", "gemini-3.5-flash")
    assert gemini_fallback_models() == []
