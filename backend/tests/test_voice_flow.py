"""POST /recovery-cases/{id}/voice/start and /voice/turn — Sarvam is always
mocked here (conftest.py nulls sarvam_api_key, and every test below patches
app.api.voice.text_to_speech/speech_to_text directly so no test depends on
that anyway). Covers: policy gate (ESCALATED blocks a call), a full turn
sequence reaching a real concluding outcome via the existing mock tools, and
the server-side turn/time cap enforcement.
"""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.db import async_session_factory, engine
from app.main import app
from app.models import Company, Contact, Invoice, RecoveryCase
from app.models.enums import CompanySegment, InvoiceStatus, RecoveryCaseStatus
from app.core.time import utc_today

NIL_UUID = "00000000-0000-0000-0000-000000000000"


@contextlib.contextmanager
def api_client():
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


async def _create_case_async(invoice_number: str, status: RecoveryCaseStatus, days_overdue: int = 5) -> str:
    async with async_session_factory() as session:
        company = Company(name=f"Voice Test Co {invoice_number}", industry="Testing", segment=CompanySegment.SMB)
        session.add(company)
        await session.flush()
        session.add(Contact(company_id=company.id, name="Fake Contact", email="fake@example.com", is_primary=True))

        due = utc_today() - timedelta(days=days_overdue)
        invoice = Invoice(
            company_id=company.id,
            invoice_number=invoice_number,
            amount_total=Decimal("30000.00"),
            amount_paid=Decimal("0.00"),
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.OVERDUE,
        )
        session.add(invoice)
        await session.flush()

        case = RecoveryCase(
            invoice_id=invoice.id,
            company_id=company.id,
            status=status,
            revenue_at_risk=invoice.amount_total,
        )
        session.add(case)
        await session.commit()
        return str(case.id)


def create_case(invoice_number: str, status: RecoveryCaseStatus, days_overdue: int = 5) -> str:
    asyncio.run(engine.dispose())
    case_id = asyncio.run(_create_case_async(invoice_number, status, days_overdue))
    asyncio.run(engine.dispose())
    return case_id


def _mock_tts():
    return patch("app.api.voice.text_to_speech", new=AsyncMock(return_value=b"fake-audio-bytes"))


def test_404_for_missing_case():
    with api_client() as client:
        resp = client.post(f"/recovery-cases/{NIL_UUID}/voice/start")
    assert resp.status_code == 404


def test_start_blocked_on_escalated_case():
    case_id = create_case("INV-VOICETEST-A001", RecoveryCaseStatus.ESCALATED)
    with _mock_tts(), api_client() as client:
        resp = client.post(f"/recovery-cases/{case_id}/voice/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is False
    assert body["ended"] is True
    assert "escalated" in body["reason"].lower()

    with api_client() as client:
        detail = client.get(f"/recovery-cases/{case_id}").json()
    voice_actions = [a for a in detail["actions"] if a["action_type"] == "PLACE_VOICE_CALL" or a["recommended_action_type"] == "PLACE_VOICE_CALL"]
    assert len(voice_actions) == 1  # the rejection itself is still audited


def test_start_on_open_case_produces_an_opening_line_and_audio():
    case_id = create_case("INV-VOICETEST-B001", RecoveryCaseStatus.OPEN)
    with _mock_tts(), api_client() as client:
        resp = client.post(f"/recovery-cases/{case_id}/voice/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["turn_number"] == 1
    assert body["agent_line"]
    assert body["agent_audio_base64"]  # TTS mocked to succeed
    assert body["ended"] is False  # opening line never concludes


def test_turn_requires_typed_reply_or_audio():
    case_id = create_case("INV-VOICETEST-C001", RecoveryCaseStatus.OPEN)
    now = datetime.now(timezone.utc).isoformat()
    with _mock_tts(), api_client() as client:
        client.post(f"/recovery-cases/{case_id}/voice/start")
        resp = client.post(
            f"/recovery-cases/{case_id}/voice/turn",
            data={"turn_number": "2", "started_at": now, "transcript_so_far": "[]"},
        )
    assert resp.status_code == 400


def test_full_turn_sequence_reaches_promise_to_pay_outcome():
    case_id = create_case("INV-VOICETEST-D001", RecoveryCaseStatus.OPEN)
    now = datetime.now(timezone.utc).isoformat()

    with _mock_tts(), api_client() as client:
        start = client.post(f"/recovery-cases/{case_id}/voice/start")
        assert start.status_code == 200

        # Rule-based fallback (no Gemini key in tests): "kal"/"haan" triggers
        # TRACK_PROMISE_TO_PAY in app.agents.llm_client._rule_based_voice_turn.
        resp = client.post(
            f"/recovery-cases/{case_id}/voice/turn",
            data={
                "turn_number": "2",
                "started_at": now,
                "transcript_so_far": "[]",
                "typed_reply": "haan main kal pay kar dunga",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ended"] is True
    assert body["outcome"]["action_type"] == "TRACK_PROMISE_TO_PAY"
    assert body["outcome"]["policy_decision"] == "APPROVED"

    with api_client() as client:
        detail = client.get(f"/recovery-cases/{case_id}").json()
    assert len(detail["promises_to_pay"]) == 1
    voice_logs = [log for log in detail["communication_logs"] if log["channel"] == "VOICE"]
    assert len(voice_logs) >= 2  # opening line + at least the customer's reply + agent's closing line


def test_turn_number_beyond_cap_ends_call_without_another_llm_call():
    case_id = create_case("INV-VOICETEST-E001", RecoveryCaseStatus.OPEN)
    now = datetime.now(timezone.utc).isoformat()

    with _mock_tts() as mock_tts, api_client() as client:
        client.post(f"/recovery-cases/{case_id}/voice/start")
        resp = client.post(
            f"/recovery-cases/{case_id}/voice/turn",
            data={
                "turn_number": "5",  # MAX_VOICE_TURNS is 4
                "started_at": now,
                "transcript_so_far": "[]",
                "typed_reply": "abhi busy hoon",
            },
        )
        calls_before = mock_tts.call_count

    assert resp.status_code == 200
    body = resp.json()
    assert body["ended"] is True
    assert body["agent_line"] is None
    # The cap check short-circuits before ever calling voice_turn/TTS again
    # for this turn (only the earlier /start call used the mock).
    assert calls_before == 1


def test_started_at_too_long_ago_ends_call():
    case_id = create_case("INV-VOICETEST-F001", RecoveryCaseStatus.OPEN)
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    with _mock_tts(), api_client() as client:
        client.post(f"/recovery-cases/{case_id}/voice/start")
        resp = client.post(
            f"/recovery-cases/{case_id}/voice/turn",
            data={
                "turn_number": "2",
                "started_at": long_ago,
                "transcript_so_far": "[]",
                "typed_reply": "haan",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["ended"] is True
