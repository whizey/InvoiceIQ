"""Synthetic unit-test outputs stay in pytest's private temporary directory."""
import json
from types import SimpleNamespace
import pytest

from app.agents import recordings
from app.agents.schemas import DiagnosisResult


def messages():
    return [SimpleNamespace(type="human", content="Unit test prompt")]


def test_minimum_context_excludes_identity_contacts_and_free_text():
    invoice, customer = recordings.minimal_context(
        {"amount_total": 100, "invoice_number": "private", "notes": "private", "email": "private"},
        {"company_name": "private", "phone": "private", "num_prior_late_payments": 2})
    assert invoice == {"amount_total": 100}
    assert customer == {"num_prior_late_payments": 2}


def test_replay_requires_independent_pin_and_rejects_tampering(tmp_path, monkeypatch):
    monkeypatch.setattr(recordings, "PRIVATE_ROOT", tmp_path)
    response = DiagnosisResult(diagnosis="Unit test only", reason="Synthetic test response", recommended_priority="low")
    provenance = recordings.record_response("diagnose", messages(), "gemini", "unit-test-model", response)
    with pytest.raises(recordings.RecordingUnavailable):
        recordings.replay_response("diagnose", messages(), DiagnosisResult)
    monkeypatch.setenv("LLM_RECORDING_ALLOWLIST", json.dumps({provenance['prompt_sha256']: provenance['integrity_sha256']}))
    actual, model = recordings.replay_response("diagnose", messages(), DiagnosisResult)
    assert actual == response
    assert model == "recorded:gemini:unit-test-model"
    assert recordings.response_provenance()['source'] == "recorded"
    path = tmp_path / f"{provenance['prompt_sha256']}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    record['response']['reason'] = 'tampered'
    path.write_text(json.dumps(record))
    with pytest.raises(recordings.RecordingUnavailable):
        recordings.replay_response("diagnose", messages(), DiagnosisResult)


def test_fallback_cannot_be_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(recordings, "PRIVATE_ROOT", tmp_path)
    with pytest.raises(recordings.RecordingUnavailable):
        recordings.record_response("diagnose", messages(), "fallback", "fallback", None)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_public_mode_does_not_call_provider_or_fabricate_fallback(monkeypatch):
    from app.agents import llm_client
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")
    monkeypatch.setenv("LLM_RECORDING_ALLOWLIST", "{}")
    monkeypatch.setattr(llm_client, "_llm_chat", lambda: pytest.fail("No provider calls in public replay"))
    with pytest.raises(recordings.RecordingUnavailable):
        await llm_client.diagnose({"days_overdue": 1}, {})


@pytest.mark.asyncio
async def test_public_startup_never_starts_scheduler_or_outbox(monkeypatch):
    from unittest.mock import AsyncMock
    from app import main
    from app.services import scheduler
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")
    monkeypatch.setattr(main.settings, "outbox_dispatch_enabled", True)
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: pytest.fail("Public startup must not start scoring/recovery jobs"))
    stop = AsyncMock()
    monkeypatch.setattr(scheduler, "stop_scheduler", stop)
    monkeypatch.setattr(main, "get_publisher", lambda: SimpleNamespace(close=AsyncMock()))
    async with main.lifespan(main.app):
        pass
    stop.assert_awaited_once()


def test_public_mutations_block_before_any_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core.public_read_only import PublicReadOnlyMiddleware
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")
    app = FastAPI()
    app.add_middleware(PublicReadOnlyMiddleware)
    @app.post("/recover")
    def recover():
        pytest.fail("Public mutation must not reach its handler")
    assert TestClient(app).post("/recover").status_code == 403
