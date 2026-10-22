"""LangSmith tracing: one nested trace per answer, customer content redacted, nothing sent when tracing is off."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agents.assistant_eval import load_fixture
from app.agents.finance_agent import Provider, answer_question
from app.core.config import settings
from app.services import langsmith_tracing
from app.services.assistant_tracing import TraceRecorder


class ToolCallingFake(GenericFakeChatModel):
    """A real LangChain chat model (so LangChain traces it) that replays scripted replies."""

    def bind_tools(self, tools, **kwargs):
        return self


def _sent(calls):
    """Every run payload the client would have sent, after its own input/output hiding."""
    runs = {}
    for method, path, body in calls:
        payload = json.loads(body)
        run_id = payload.get("id") or path.rstrip("/").split("/")[-1]
        runs.setdefault(run_id, {}).update({k: v for k, v in payload.items() if v is not None})
    return list(runs.values())


@pytest.fixture
def captured(monkeypatch):
    """Tracing on, with clients that record their HTTP requests instead of reaching LangSmith."""
    from langsmith import Client

    calls = []
    monkeypatch.setattr(settings, "langchain_tracing_v2", True)
    monkeypatch.setattr(settings, "langchain_api_key", "test-key")
    monkeypatch.setattr(settings, "langsmith_log_customer_content", False)
    monkeypatch.setattr(langsmith_tracing, "_clients", {})
    real_client = langsmith_tracing.client

    class RecordingClient(Client):
        def request_with_retries(self, method, path, *args, request_kwargs=None, **kwargs):
            body = (request_kwargs or {}).get("data") or b"{}"
            calls.append((method, path, body.decode() if isinstance(body, bytes) else body))
            return SimpleNamespace(status_code=200, json=lambda: {}, raise_for_status=lambda: None)

    def offline_client(redact):
        if redact not in langsmith_tracing._clients:
            hide = (lambda _payload: dict(langsmith_tracing.REDACTED)) if redact else None
            langsmith_tracing._clients[redact] = RecordingClient(
                api_key="test-key", api_url="http://langsmith.invalid", auto_batch_tracing=False,
                hide_inputs=hide, hide_outputs=hide)
        return langsmith_tracing._clients[redact]

    monkeypatch.setattr(langsmith_tracing, "client", offline_client)
    yield calls
    monkeypatch.setattr(langsmith_tracing, "client", real_client)


def _answer(workspace, question="How much is overdue?"):
    portfolio = load_fixture()[0]
    portfolio.workspace = workspace
    llm = ToolCallingFake(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "get_portfolio_summary", "args": {}, "id": "t1"}]),
        AIMessage(content="Overdue is ₹15.2 lakh across 7 invoices.")]))
    return asyncio.run(answer_question(portfolio, question, [], [Provider("gemini", "fake-model", llm)],
                                       TraceRecorder()))


def test_one_nested_trace_per_answer_with_metadata(captured):
    result = _answer(SimpleNamespace(id="ws-1", name="Sample", kind="showcase"))
    assert result.answer == "Overdue is ₹15.2 lakh across 7 invoices."
    runs = _sent(captured)
    by_name = {}
    for run in runs:
        by_name.setdefault(run["name"], []).append(run)
    root = by_name["assistant_answer"][0]
    assert {"get_portfolio_summary", "check_figures", "check_written_advice"} <= set(by_name)
    assert any(r.get("run_type") == "llm" for r in runs)  # LangChain's own model runs nest in the same trace
    assert all(r.get("trace_id") == root["id"] for r in runs)
    meta = root["extra"]["metadata"]
    assert (meta["answered_by"], meta["model"], meta["figures_checked"], meta["workspace_kind"]) == (
        "primary", "fake-model", 2, "showcase")
    assert meta["content_redacted"] is False and meta["workspace"] != "ws-1"  # hashed, never the raw id
    assert root["inputs"] == {"question": "How much is overdue?"}
    assert "15.2 lakh" in json.dumps(root["outputs"])
    tool = by_name["get_portfolio_summary"][0]
    assert tool["run_type"] == "tool" and "1520000" in json.dumps(tool["outputs"])


def test_customer_content_never_leaves_the_server(captured):
    _answer(SimpleNamespace(id="ws-2", name="Acme Books", kind="customer"), question="What does Acme owe us?")
    runs = _sent(captured)
    assert runs, "the structure is still traced"
    flat = json.dumps(runs)
    for secret in ("Acme", "What does Acme owe", "15.2 lakh", "1520000", "Overdue is"):
        assert secret not in flat, secret
    root = next(r for r in runs if r["name"] == "assistant_answer")
    assert root["extra"]["metadata"]["content_redacted"] is True
    assert root["extra"]["metadata"]["answered_by"] == "primary"  # metadata still useful for filtering


def test_customer_content_is_sent_only_when_the_operator_opts_in(captured, monkeypatch):
    monkeypatch.setattr(settings, "langsmith_log_customer_content", True)
    _answer(SimpleNamespace(id="ws-3", name="Acme Books", kind="customer"), question="What does Acme owe us?")
    assert "What does Acme owe" in json.dumps(_sent(captured))


def test_nothing_is_sent_when_tracing_is_off(captured, monkeypatch):
    monkeypatch.setattr(settings, "langchain_tracing_v2", False)
    result = _answer(SimpleNamespace(id="ws-4", name="Sample", kind="showcase"))
    assert result.answer.startswith("Overdue is") and captured == []


def test_a_broken_langsmith_never_breaks_an_answer(monkeypatch):
    monkeypatch.setattr(settings, "langchain_tracing_v2", True)
    monkeypatch.setattr(settings, "langchain_api_key", "test-key")
    monkeypatch.setattr(langsmith_tracing, "client", lambda redact: (_ for _ in ()).throw(RuntimeError("down")))
    result = _answer(SimpleNamespace(id="ws-5", name="Acme Books", kind="customer"))
    assert result.answer.startswith("Overdue is")


def test_untraced_calls_send_nothing_even_with_global_tracing(captured, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    llm = GenericFakeChatModel(messages=iter([AIMessage(content="transcript")]))
    with langsmith_tracing.untraced():
        assert asyncio.run(llm.ainvoke("audio")).content == "transcript"
    assert captured == []
