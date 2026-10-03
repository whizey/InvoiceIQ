import json
import logging
import os

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.observability import JSONFormatter, configure_langsmith, request_id_var
from app.main import app


def _record(msg="hi", args=(), **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_core_fields():
    payload = json.loads(JSONFormatter().format(_record("hello %s", ("world",))))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    assert "request_id" not in payload


def test_json_formatter_includes_request_id_when_bound():
    token = request_id_var.set("abc-123")
    try:
        payload = json.loads(JSONFormatter().format(_record()))
        assert payload["request_id"] == "abc-123"
    finally:
        request_id_var.reset(token)


def test_json_formatter_omits_request_id_when_not_bound():
    # No request_id_var.set() in this test — simulates a log line emitted
    # outside any HTTP request (e.g. a CLI script).
    payload = json.loads(JSONFormatter().format(_record()))
    assert "request_id" not in payload


def test_json_formatter_includes_arbitrary_extra_fields():
    payload = json.loads(JSONFormatter().format(_record(case_id="case-1", duration_ms=12.3)))
    assert payload["case_id"] == "case-1"
    assert payload["duration_ms"] == 12.3


def test_configure_langsmith_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "langchain_tracing_v2", False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    configure_langsmith()
    assert "LANGCHAIN_TRACING_V2" not in os.environ


def test_configure_langsmith_sets_env_vars_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "langchain_tracing_v2", True)
    monkeypatch.setattr(settings, "langchain_api_key", "ls-test-key")
    monkeypatch.setattr(settings, "langchain_project", "test-project")
    try:
        configure_langsmith()
        assert os.environ["LANGCHAIN_TRACING_V2"] == "true"
        assert os.environ["LANGCHAIN_API_KEY"] == "ls-test-key"
        assert os.environ["LANGCHAIN_PROJECT"] == "test-project"
    finally:
        for var in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT"):
            monkeypatch.delenv(var, raising=False)


def test_request_id_header_present_and_echoed():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert "x-request-id" in resp.headers

        resp2 = client.get("/health", headers={"X-Request-ID": "my-custom-id"})
        assert resp2.headers["x-request-id"] == "my-custom-id"
