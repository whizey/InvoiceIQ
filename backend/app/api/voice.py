"""Hinglish voice-call demo — Sarvam does speech I/O only (see
app/tools/sarvam_client.py); app/agents/llm_client.voice_turn() decides what
to say and what to *propose*; evaluate_policy() (app/services/policy_engine.py)
decides what actually happens, exactly like the LangGraph workflow does for
every other action. PLACE_VOICE_CALL is human-triggered only — the scheduler
(app/services/scheduler.py) never calls anything in this module (see
tests/test_scheduler_safety.py).

No persisted "voice session" table: turn count and elapsed time are tracked
by the client and validated here per request (turn_number/started_at) —
every turn still writes a real CommunicationLog row, so the audit trail is
complete even without a session concept. See the project plan notes for the
trade-off (a determined API caller could fake turn_number; acceptable for a
single-user local demo with no auth anywhere else in this app either).
"""

import base64
import json
import logging
from datetime import datetime, timezone
from uuid import UUID
from app.core.time import utc_today


from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_client import MAX_VOICE_TURNS, voice_turn
from app.agents.schemas import VoiceTurnResult
from app.core.db import get_db
from app.core.locks import LockAcquisitionError, acquire_lock
from app.models import AuditLog, Company, Contact, CommunicationLog, Invoice, RecoveryCase
from app.models.enums import (
    AuditActor,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationStatus,
    ProposedBy,
    RecoveryActionStatus,
    RecoveryActionType,
)
from app.schemas.voice import VoiceOutcomeOut, VoiceStartOut, VoiceTurnOut
from app.services.action_policy import (
    evaluate_and_record_action,
    evaluate_policy_for_case,
    primary_contact,
)
from app.tools.mock_tools import execute_mock_action
from app.tools.sarvam_client import speech_to_text, text_to_speech

router = APIRouter(prefix="/recovery-cases", tags=["voice"])
logger = logging.getLogger("app.voice")

MAX_VOICE_CALL_SECONDS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _voice_contexts(invoice: Invoice, company_name: str) -> tuple[dict, dict]:
    days_overdue = max((utc_today() - invoice.due_date).days, 0)
    invoice_context = {
        "invoice_number": invoice.invoice_number,
        "amount_total": float(invoice.amount_total),
        "days_overdue": days_overdue,
        "currency": invoice.currency,
    }
    customer_context = {"company_name": company_name}
    return invoice_context, customer_context


async def _speak_turn(
    db: AsyncSession,
    case: RecoveryCase,
    invoice: Invoice,
    contact: Contact | None,
    company_name: str,
    transcript_so_far: list[dict],
    turn_number: int,
) -> tuple[VoiceTurnResult, str | None, VoiceOutcomeOut | None]:
    """Produce one agent line (+ optional TTS audio), log it, and — only if
    the turn concludes with a real proposed_action — run that action through
    evaluate_policy() and the existing mock tools. Never executes a tool for
    "NONE" or a not-yet-concluded turn."""
    invoice_context, customer_context = _voice_contexts(invoice, company_name)
    result, model_name = await voice_turn(invoice_context, customer_context, transcript_so_far, turn_number)

    audio_bytes = await text_to_speech(result.agent_line_hi)
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii") if audio_bytes else None

    db.add(
        CommunicationLog(
            recovery_case_id=case.id,
            contact_id=contact.id if contact else None,
            channel=CommunicationChannel.VOICE,
            direction=CommunicationDirection.OUTBOUND,
            body=result.agent_line_hi,
            status=CommunicationStatus.SENT if audio_b64 else CommunicationStatus.SIMULATED,
            sent_at=_now(),
        )
    )
    db.add(
        AuditLog(
            recovery_case_id=case.id,
            entity_type="recovery_case",
            entity_id=case.id,
            event_type="VOICE_TURN",
            actor=AuditActor.AI_AGENT,
            description=f"Voice agent (turn {turn_number}, {model_name}): {result.agent_line_hi}",
            occurred_at=_now(),
        )
    )
    await db.flush()

    outcome_out: VoiceOutcomeOut | None = None
    if result.concluded and result.proposed_action != "NONE":
        recommended = RecoveryActionType(result.proposed_action)
        recorded = await evaluate_and_record_action(db, case, invoice, recommended, proposed_by=ProposedBy.HUMAN)
        action = recorded.action
        outcome = recorded.outcome
        exec_result = await execute_mock_action(db, outcome.final_action, case, invoice, contact)
        action.status = RecoveryActionStatus.EXECUTED
        action.executed_at = _now()
        action.result = exec_result
        await db.flush()
        db.add(
            AuditLog(
                recovery_case_id=case.id,
                entity_type="recovery_action",
                entity_id=action.id,
                event_type="VOICE_CALL_CONCLUDED",
                actor=AuditActor.SYSTEM,
                description=f"Voice call concluded: policy {outcome.decision.value} {outcome.final_action.value}.",
                occurred_at=_now(),
            )
        )
        outcome_out = VoiceOutcomeOut(
            action_type=outcome.final_action, policy_decision=outcome.decision, reason=outcome.reason
        )

    return result, audio_b64, outcome_out


@router.post("/{case_id}/voice/start", response_model=VoiceStartOut)
async def start_voice_call(case_id: UUID, db: AsyncSession = Depends(get_db)) -> VoiceStartOut:
    """Human-triggered only (case-page "Start Hinglish recovery call" button).
    Gated by evaluate_policy() exactly like every other action — blocked if
    the case is already ESCALATED. Returns 409 if a cycle/other action is
    already running for this case."""
    case = await db.get(RecoveryCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    try:
        async with acquire_lock(f"recovery-case:{case_id}"):
            invoice = await db.get(Invoice, case.invoice_id)
            company = await db.get(Company, case.company_id)
            contact = await primary_contact(db, case.company_id)

            recorded = await evaluate_and_record_action(
                db, case, invoice, RecoveryActionType.PLACE_VOICE_CALL, proposed_by=ProposedBy.HUMAN
            )
            action = recorded.action
            outcome = recorded.outcome

            if outcome.final_action != RecoveryActionType.PLACE_VOICE_CALL:
                action.status = RecoveryActionStatus.EXECUTED
                action.executed_at = _now()
                action.result = {"started": False, "reason": outcome.reason}
                await db.commit()
                return VoiceStartOut(
                    started=False, turn_number=0, agent_line=None, agent_audio_base64=None,
                    ended=True, reason=outcome.reason,
                )

            action.status = RecoveryActionStatus.EXECUTED
            action.executed_at = _now()
            action.result = {"started": True}
            await db.flush()

            result, audio_b64, _outcome = await _speak_turn(
                db, case, invoice, contact, company.name, [], turn_number=1
            )
            await db.commit()
    except LockAcquisitionError:
        raise HTTPException(
            status_code=409, detail="A recovery cycle is already running for this case"
        ) from None

    return VoiceStartOut(
        started=True, turn_number=1, agent_line=result.agent_line_hi, agent_audio_base64=audio_b64,
        ended=result.concluded,
    )


@router.post("/{case_id}/voice/turn", response_model=VoiceTurnOut)
async def submit_voice_turn(
    case_id: UUID,
    turn_number: int = Form(...),
    started_at: str = Form(...),
    transcript_so_far: str = Form("[]"),
    typed_reply: str | None = Form(None),
    audio: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
) -> VoiceTurnOut:
    """One round of the call: the customer's reply (typed, or recorded audio
    transcribed via Sarvam STT) in, the next agent line out. Caps enforced
    server-side regardless of what the client sends: turn_number beyond
    MAX_VOICE_TURNS, or started_at more than MAX_VOICE_CALL_SECONDS ago, ends
    the call without another LLM/Sarvam call."""
    case = await db.get(RecoveryCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    # Re-gate on every turn, not just at /voice/start.
    #
    # A call spans several HTTP requests and only the first one used to be
    # policy-checked, which made the gate skippable: POST straight here
    # with turn_number=1 and a fresh started_at, never calling
    # /voice/start, and the conversation ran on a case the policy engine
    # had refused to call -- writing CommunicationLogs and concluding into
    # an executed TRACK_PROMISE_TO_PAY on an ESCALATED case a human had
    # already taken over. The module docstring claimed the policy gate
    # held here; it did not.
    #
    # Re-checking also handles the case changing mid-call (escalated by a
    # concurrent cycle, or a promise breaking), which should stop the call
    # rather than let it finish against current policy.
    invoice_for_policy = await db.get(Invoice, case.invoice_id)
    gate = await evaluate_policy_for_case(
        db, case, invoice_for_policy, RecoveryActionType.PLACE_VOICE_CALL
    )
    if gate.final_action != RecoveryActionType.PLACE_VOICE_CALL:
        return VoiceTurnOut(
            turn_number=turn_number,
            transcript_user="",
            agent_line=None,
            agent_audio_base64=None,
            ended=True,
            reason=gate.reason,
        )

    if not typed_reply and audio is None:
        raise HTTPException(status_code=400, detail="Provide either typed_reply or a recorded audio file.")

    # transcript_so_far is client-controlled form input, so its SHAPE has to
    # be checked, not just its JSON validity. Catching ValueError/TypeError
    # alone let valid-but-wrong JSON straight through: transcript_so_far=5
    # parsed fine, then `[*history, ...]` below raised an uncaught TypeError
    # (500), and [{"a": 1}] reached prompts.py and raised KeyError:
    # 'speaker' once an LLM key was configured. Malformed input should be
    # ignored, not crash the call.
    history: list[dict] = []
    try:
        parsed = json.loads(transcript_so_far)
    except (ValueError, TypeError):
        parsed = None

    if isinstance(parsed, list):
        history = [
            turn
            for turn in parsed
            if isinstance(turn, dict)
            and isinstance(turn.get("speaker"), str)
            and isinstance(turn.get("text"), str)
        ]

    try:
        started_dt = datetime.fromisoformat(started_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="started_at must be an ISO 8601 timestamp") from None
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=timezone.utc)

    elapsed = (_now() - started_dt).total_seconds()
    if turn_number > MAX_VOICE_TURNS or elapsed > MAX_VOICE_CALL_SECONDS:
        return VoiceTurnOut(
            turn_number=turn_number, transcript_user="", agent_line=None, agent_audio_base64=None, ended=True,
        )

    try:
        async with acquire_lock(f"recovery-case:{case_id}"):
            invoice = await db.get(Invoice, case.invoice_id)
            company = await db.get(Company, case.company_id)
            contact = await primary_contact(db, case.company_id)

            if typed_reply:
                user_text = typed_reply
            else:
                audio_bytes = await audio.read()
                user_text = await speech_to_text(
                    audio_bytes, audio.filename or "reply.webm", audio.content_type or "audio/webm"
                )
                if not user_text:
                    # No Sarvam key, or STT failed — simulated fallback so the
                    # demo still works (browser mic/Sarvam issues shouldn't crash it).
                    user_text = "(simulated reply — no speech-to-text available)"

            db.add(
                CommunicationLog(
                    recovery_case_id=case.id,
                    contact_id=contact.id if contact else None,
                    channel=CommunicationChannel.VOICE,
                    direction=CommunicationDirection.INBOUND,
                    body=user_text,
                    status=CommunicationStatus.SIMULATED,
                    sent_at=_now(),
                )
            )
            await db.flush()

            history = [*history, {"speaker": "customer", "text": user_text}]
            result, audio_b64, outcome_out = await _speak_turn(
                db, case, invoice, contact, company.name, history, turn_number
            )
            await db.commit()
    except LockAcquisitionError:
        raise HTTPException(
            status_code=409, detail="A recovery cycle is already running for this case"
        ) from None

    return VoiceTurnOut(
        turn_number=turn_number,
        transcript_user=user_text,
        agent_line=result.agent_line_hi,
        agent_audio_base64=audio_b64,
        ended=result.concluded,
        outcome=outcome_out,
    )
