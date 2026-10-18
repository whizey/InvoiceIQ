"""Only actual provider attempts are counted as calls; labels stay bounded."""

from unittest.mock import AsyncMock

import pytest

from app.agents.llm_client import _invoke_structured_llm, _log_llm_call
from app.agents.schemas import InterventionRecommendation
from app.core.config import settings
from app.core.metrics import LLM_CALLS, LLM_FALLBACKS


@pytest.mark.asyncio
async def test_provider_outcomes_and_fallback_are_distinct(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    success = LLM_CALLS.labels("recommend", "openai", "success")._value.get()
    invalid = LLM_CALLS.labels("recommend", "openai", "invalid")._value.get()
    error = LLM_CALLS.labels("recommend", "openai", "error")._value.get()
    fallback = LLM_FALLBACKS.labels("recommend", "openai")._value.get()

    async def invoke(response):
        return await _invoke_structured_llm(
            AsyncMock(ainvoke=AsyncMock(return_value=response)), [],
            stage="recommend", provider="openai", model_name="test-model",
            schema=InterventionRecommendation,
        )

    assert (await invoke({"action": "WAIT", "rationale": "No action."})).action == "WAIT"
    assert await invoke({"action": "TRANSFER_FUNDS", "rationale": "Invalid"}) is None
    broken = AsyncMock(ainvoke=AsyncMock(side_effect=TimeoutError))
    assert await _invoke_structured_llm(
        broken, [], stage="recommend", provider="openai", model_name="test-model",
        schema=InterventionRecommendation,
    ) is None
    _log_llm_call("recommend", "openai", "rule-based-fallback", llm_called=False)

    assert LLM_CALLS.labels("recommend", "openai", "success")._value.get() == success + 1
    assert LLM_CALLS.labels("recommend", "openai", "invalid")._value.get() == invalid + 1
    assert LLM_CALLS.labels("recommend", "openai", "error")._value.get() == error + 1
    assert LLM_FALLBACKS.labels("recommend", "openai")._value.get() == fallback + 1


def test_untrusted_telemetry_labels_collapse_to_other(monkeypatch):
    from app.core.metrics import record_llm_call

    monkeypatch.setattr(settings, "metrics_enabled", True)
    before = LLM_CALLS.labels("other", "other", "other")._value.get()
    record_llm_call("invoice-123", "customer-name", "secret", 0.01)
    assert LLM_CALLS.labels("other", "other", "other")._value.get() == before + 1
