"""Synthetic provider doubles test safety, never genuine-provider acceptance."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import date
import pytest
from app.agents import recordings
from app.agents import record_gemini as cli
from app.agents.schemas import DiagnosisResult, InterventionRecommendation


@pytest.fixture
def private_config(tmp_path, monkeypatch):
    root, pins = tmp_path / "recordings", tmp_path / "pins" / "pins.json"
    monkeypatch.setenv("LLM_RECORDINGS_DIR", str(root))
    monkeypatch.setenv("LLM_RECORDING_ALLOWLIST_FILE", str(pins))
    monkeypatch.setenv("GOOGLE_API_KEY", "unit-test-not-a-credential")
    return root, pins


def context():
    return {
        "invoice": {"amount_total": 1200000, "amount_paid": 0, "days_overdue": 3},
        "customer": {"num_prior_late_payments": 1},
        "historical_as_of": "2015-02-03",
        "reminder_count": 0,
    }


@pytest.mark.asyncio
async def test_capture_private_pins_metadata_and_network_free_replay(private_config, monkeypatch):
    root, pins = private_config
    response = DiagnosisResult(
        diagnosis="Synthetic unit output", reason="Unit test only", recommended_priority="low"
    )
    raw = SimpleNamespace(
        id="unit-test-response",
        usage_metadata={"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
        response_metadata={"model_name": "unit-test-model", "finish_reason": "STOP"},
    )
    invoke = AsyncMock(return_value={"parsed": response, "raw": raw, "parsing_error": None})
    client = SimpleNamespace(with_structured_output=lambda *a, **kw: SimpleNamespace(ainvoke=invoke))
    result = await cli.capture_stage("diagnose", client, "unit-test-model", context(), None, pins)
    assert result == response
    invoke.assert_awaited_once()
    messages = cli.messages_for("diagnose", context()["invoice"], context()["customer"])
    path = root / (recordings.prompt_hash("diagnose", messages) + ".json")
    record = json.loads(path.read_text())
    assert record["response_sha256"] == recordings.digest(response.model_dump())
    assert record["redacted_context_sha256"] == recordings.digest(context())
    assert record["request_metadata"]["usage"]["total_tokens"] == 18
    assert record["schema_validation"] == "passed" and record["provider"] == "gemini"
    assert path.stat().st_mode & 0o777 == pins.stat().st_mode & 0o777 == 0o600
    assert "unit-test-not-a-credential" not in path.read_text()
    monkeypatch.setenv("LLM_MODE", "recorded")
    from app.agents import llm_client

    monkeypatch.setattr(llm_client, "_llm_chat", lambda: pytest.fail("Replay cannot call a provider"))
    actual, model = await llm_client.diagnose(context()["invoice"], context()["customer"])
    assert actual == response and model == "recorded:gemini:unit-test-model"
    with pytest.raises(recordings.RecordingUnavailable):
        await llm_client.diagnose({"amount_total": 1200001}, context()["customer"])
    record["response"]["reason"] = "tampered"
    path.write_text(json.dumps(record))
    with pytest.raises(recordings.RecordingUnavailable):
        await llm_client.diagnose(context()["invoice"], context()["customer"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        None,
        {"action": "WAIVE_INVOICE", "rationale": "forbidden"},
        {"action": "WAIT", "rationale": "x", "tool_call": "email"},
        {"action": "WAIT", "rationale": "Email customer@example.com"},
    ],
)
async def test_invalid_or_sensitive_output_is_never_recorded(private_config, output):
    root, pins = private_config
    invoke = AsyncMock(return_value={"parsed": output, "raw": SimpleNamespace(), "parsing_error": None})
    client = SimpleNamespace(with_structured_output=lambda *a, **kw: SimpleNamespace(ainvoke=invoke))
    with pytest.raises(cli.CaptureError):
        await cli.capture_stage("recommend", client, "unit-test-model", context(), {}, pins)
    assert not pins.exists() and not root.exists()
    invoke.assert_awaited_once()


def test_forbidden_pinned_action_rejected_by_replay_schema(private_config):
    root, pins = private_config
    messages = cli.messages_for("recommend", {}, {}, {}, 0)
    valid = InterventionRecommendation(action="WAIT", rationale="Synthetic unit test only")
    provenance = recordings.record_response("recommend", messages, "gemini", "unit-test-model", valid)
    path = root / (provenance["prompt_sha256"] + ".json")
    record = json.loads(path.read_text())
    record.pop("integrity_sha256")
    record["response"]["action"] = "WAIVE_INVOICE"
    record["response_sha256"] = recordings.digest(record["response"])
    fingerprint = recordings.digest(record)
    path.write_text(json.dumps({**record, "integrity_sha256": fingerprint}))
    cli.write_private(pins, {provenance["prompt_sha256"]: fingerprint})
    with pytest.raises(recordings.RecordingUnavailable):
        recordings.replay_response("recommend", messages, InterventionRecommendation)


def test_private_paths_require_separate_outside_git_trust_root(tmp_path):
    with pytest.raises(cli.CaptureError):
        cli.private_paths(cli.REPO / ".private", tmp_path / "pins.json")
    with pytest.raises(cli.CaptureError):
        cli.private_paths(tmp_path, tmp_path / "pins.json")


def test_database_guard_rejects_development_database(monkeypatch):
    monkeypatch.setenv("PHASE1_TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5435/recovery_db")
    with pytest.raises(cli.CaptureError, match="isolated"):
        cli.configure_database()


@pytest.mark.asyncio
async def test_genuine_private_capture_through_real_graph():
    if os.getenv("TEST_GENUINE_REPLAY") != "1":
        pytest.skip(
            "BLOCKED pending user-authorized genuine Gemini capture; synthetic output cannot satisfy acceptance"
        )
    command = [
        sys.executable,
        "-m",
        "app.agents.record_gemini",
        "verify",
        "--isolated-container",
        "--case-id",
        os.environ["GENUINE_REPLAY_CASE_ID"],
        "--as-of",
        os.environ["GENUINE_REPLAY_AS_OF"],
        "--recordings-dir",
        os.environ["LLM_RECORDINGS_DIR"],
        "--pins-file",
        os.environ["LLM_RECORDING_ALLOWLIST_FILE"],
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OFFLINE_REPLAY_PASS:" in result.stdout


@pytest.mark.asyncio
async def test_real_historical_context_and_recorded_graph_policy_rollback(private_config, monkeypatch):
    """Actual PostgreSQL/graph; SYNTHETIC recordings exercise mechanics only."""
    from sqlalchemy import select, func
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.models import RecoveryCase, AgentDecision, AuditLog, RecoveryAction
    from app.core.time import historical_as_of
    from app.agents import graph, llm_client

    configured = os.getenv("PHASE1_REPLAY_DATABASE_URL")
    if not configured:
        pytest.skip("Requires existing isolated historical PostgreSQL cohort")
    url = make_url(configured)
    assert url.host == "127.0.0.1" and url.port == 55436
    assert url.database == "invoiceiq_phase1_replay_test"
    engine = create_async_engine(url)
    root, pins = private_config
    try:
        with historical_as_of(date(2015, 2, 3)):
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                cases = (await session.scalars(select(RecoveryCase).order_by(RecoveryCase.id))).all()
                assert len(cases) == 3
                before = {
                    m.__tablename__: await session.scalar(select(func.count()).select_from(m))
                    for m in [AgentDecision, AuditLog, RecoveryAction]
                }
                case = next(c for c in cases if float(c.revenue_at_risk) < 1000000)
                case_id = case.id
                await session.rollback()
                safe = await cli.historical_context(session, case_id, date(2015, 2, 3))
                await session.rollback()
                assert safe["invoice"]["amount_paid"] == 0
                assert safe["invoice"]["posting_date"] <= safe["historical_as_of"]
                assert "company_name" not in safe["customer"] and "Clearing_date" not in safe["invoice"]
                diagnosis = DiagnosisResult(
                    diagnosis="Synthetic database test only",
                    reason="Not a genuine provider result",
                    recommended_priority="low",
                )
                recommendation = InterventionRecommendation(
                    action="SEND_EMAIL", rationale="Synthetic cooldown rejection test only"
                )
                allowlist = {}
                for stage, response in [("diagnose", diagnosis), ("recommend", recommendation)]:
                    messages = cli.messages_for(
                        stage,
                        safe["invoice"],
                        safe["customer"],
                        diagnosis.model_dump(),
                        safe["reminder_count"],
                    )
                    provenance = recordings.record_response(
                        stage, messages, "gemini", "unit-test-model", response, context=safe
                    )
                    allowlist[provenance["prompt_sha256"]] = provenance["integrity_sha256"]
                cli.write_private(pins, allowlist)
                # Freeze the policy clock at an actual stored workflow timestamp;
                # source dates remain untouched and cooldown verification is deterministic.
                from app.core import time as clock

                last_action_at = await graph._last_action_at(session, case_id)
                assert last_action_at is not None
                monkeypatch.setattr(clock, "utc_now", lambda: last_action_at)
                monkeypatch.setenv("LLM_MODE", "recorded")
                monkeypatch.setattr(
                    llm_client, "_llm_chat", lambda: pytest.fail("Offline graph attempted provider call")
                )
                # Match the offline recording verifier: allocate a run UUID and
                # invoke the graph directly so the caller can roll back all writes.
                from uuid import UUID, uuid4

                investigation_id = str(uuid4())
                state = await graph._build_graph(session).ainvoke(
                    {"case_id": str(case_id), "investigation_id": investigation_id}
                )
                assert str(UUID(state["investigation_id"])) == investigation_id
                assert state["recommended_action"] == "SEND_EMAIL"
                assert state["policy_decision"] == "REJECTED" and state["final_action"] == "WAIT"
                assert state["action_result"]["simulated"] is True
                new = (
                    await session.scalars(
                        select(AgentDecision).where(
                            AgentDecision.model_name == "recorded:gemini:unit-test-model"
                        )
                    )
                ).all()
                assert len(new) == 2
                assert all(d.output["provenance"]["source"] == "recorded" for d in new)
                assert all(d.output["investigation_id"] == state["investigation_id"] for d in new)
                await session.rollback()
                after = {
                    m.__tablename__: await session.scalar(select(func.count()).select_from(m))
                    for m in [AgentDecision, AuditLog, RecoveryAction]
                }
                assert after == before
    finally:
        await engine.dispose()
        # Diagnosis uses the existing scoped read-only-query session factory.
        # Dispose its pool before pytest moves to another event loop.
        from app.core.db import engine as query_engine

        await query_engine.dispose()


@pytest.mark.asyncio
async def test_provider_timeout_does_not_retry_or_record(private_config):
    root, pins = private_config
    invoke = AsyncMock(side_effect=TimeoutError("synthetic timeout"))
    client = SimpleNamespace(with_structured_output=lambda *a, **kw: SimpleNamespace(ainvoke=invoke))
    with pytest.raises(TimeoutError):
        await cli.capture_stage("diagnose", client, "unit-test-model", context(), None, pins)
    invoke.assert_awaited_once()
    assert not root.exists() and not pins.exists()


@pytest.mark.asyncio
async def test_capture_requires_explicit_authorization_before_database_access(monkeypatch):
    monkeypatch.setattr(
        cli, "configure_database", lambda *a: pytest.fail("No DB access before authorization")
    )
    with pytest.raises(cli.CaptureError, match="confirm-provider-calls"):
        await cli.run(SimpleNamespace(command="capture", confirm_provider_calls=False))


def test_sdk_failure_text_never_leaks_to_cli_output(monkeypatch, capsys):
    async def failure(_args):
        raise RuntimeError("synthetic-private-request-details")

    monkeypatch.setattr(cli, "run", failure)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "record_gemini",
            "verify",
            "--case-id",
            "edd05c6b-2c8a-4fb0-a60b-6fd7a94fd083",
            "--as-of",
            "2015-02-03",
        ],
    )
    assert cli.main() == 1
    output = capsys.readouterr()
    assert "BLOCKED:" in output.err
    assert "synthetic-private-request-details" not in output.err + output.out
