"""Keep local .env from changing the deterministic test path.

Workflow tests assert the rule-based fallback's first-cycle SEND_EMAIL
behavior. A real GOOGLE_API_KEY or SCHEDULER_ENABLED in backend/.env must
not make tests call Gemini or start the live loop — and a real
RESEND_API_KEY or SARVAM_API_KEY must not make tests send a real email or
call Sarvam. Every test that needs those providers mocks them explicitly.
"""

from app.core.config import settings

settings.scheduler_enabled = False

import pytest


@pytest.fixture(autouse=True)
def disable_external_llm(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "llm_model", None)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "resend_api_key", None)
    monkeypatch.setattr(settings, "resend_from", None)
    monkeypatch.setattr(settings, "sarvam_api_key", None)
