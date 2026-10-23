"""Keep local .env from changing the deterministic test path.

Workflow tests assert the rule-based fallback's first-cycle SEND_EMAIL
behavior. A real GOOGLE_API_KEY or SCHEDULER_ENABLED in backend/.env must
not make tests call Gemini or start the live loop — and a real
RESEND_API_KEY or SARVAM_API_KEY must not make tests send a real email or
call Sarvam. Every test that needs those providers mocks them explicitly.
"""

import os

# Several integration fixtures deliberately clear and reseed tables. Never let
# a developer's application .env choose the database receiving those writes.
# Fixed endpoint, never inherited from the application's development .env.
from scripts.test_database_safety import install_connection_guard, validate_url

test_url = validate_url(os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://invoiceiq_ci:unconfigured@127.0.0.1:55436/invoiceiq_test",
))
os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
os.environ["INVOICEIQ_TEST_NULL_POOL"] = "1"

from app.core.config import settings

settings.database_url = test_url.render_as_string(hide_password=False)
install_connection_guard(settings.database_url)

settings.scheduler_enabled = False
settings.outbox_dispatch_enabled = False
settings.langchain_tracing_v2 = False
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

import pytest


@pytest.fixture(autouse=True)
def disable_external_llm(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(settings, "groq_model", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "langchain_api_key", None)
    monkeypatch.setattr(settings, "langchain_tracing_v2", False)
    monkeypatch.setattr(settings, "eval_google_api_key", None)
    monkeypatch.setattr(settings, "eval_groq_api_key", None)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "outbox_dispatch_enabled", False)
    monkeypatch.setattr(settings, "kafka_bootstrap_servers", None)
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(settings, "resend_api_key", None)
    monkeypatch.setattr(settings, "resend_from", None)
    monkeypatch.setattr(settings, "sarvam_api_key", None)
    monkeypatch.setattr(settings, "zoho_client_id", None)
    monkeypatch.setattr(settings, "zoho_client_secret", None)
    monkeypatch.setattr(settings, "zoho_redirect_uri", None)
