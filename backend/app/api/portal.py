"""Isolated product portal. Never use portal identities for legacy operator routes."""
import asyncio
import base64
import binascii
import csv
import hashlib
import io
import json
import hmac
import os
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.db import get_db
from app.core.login_throttle import login_throttle, registration_limit
from app.models.portal import (
    PortalWorkspace, PortalUser, PortalMembership, PortalSession, PortalInvoice, PortalCaseEvent,
    PortalPayment, PortalNote,
)
from app.ml.champion_serving import verified_contract
from app.ml.payment_risk_model import PaymentRiskArtifactError
from app.services.portal_risk import explain_invoice, provenance, score_workspace
from app.events import topics as event_topics
from app.events import get_publisher
from app.events.inline import deliver_event
from app.events.outbox import TransactionalEventPublisher
from app.core.db import async_session_factory
from app.models.event_effects import ApprovalAlert
from app.models.outbox import OutboxEvent
from app.services.portal_recovery import SHOWCASE_BASELINE_SCOPE, recovery_summary

router = APIRouter(prefix="/portal", tags=["customer portal"])
SHOWCASE_ID = uuid.UUID("60c111be-dde9-4a54-9dcc-f881d0d0aaa1")
SEED_VERSION = "showcase-v1"

# Read cache; any write to a workspace bumps its version, so views never go stale. The TTL only bounds memory and
# the date rollover, so it can be generous.
CACHE_TTL_SECONDS = 300
_cache: dict[tuple, tuple[float, object]] = {}
_versions: dict[uuid.UUID, int] = {}


def _bump(workspace_id: uuid.UUID) -> None:
    _versions[workspace_id] = _versions.get(workspace_id, 0) + 1


async def _cached(workspace, session, name: str, extra, build):
    key = (workspace.id, _versions.get(workspace.id, 0), session.token_digest if workspace.kind == "showcase" else "", name, extra)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    value = await build()
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)
    return value


class RegisterInput(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    full_name: str = Field(min_length=2, max_length=160)
    company: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=12, max_length=256)


class LoginInput(BaseModel):
    email: str
    password: str


class DecisionInput(BaseModel):
    invoice_id: uuid.UUID
    decision: str
    note: str = Field(min_length=3, max_length=500)


NOTE_KINDS = ("customer_message", "dispute", "promise", "internal")


class NoteInput(BaseModel):
    invoice_id: uuid.UUID
    kind: str = Field(pattern="^(" + "|".join(NOTE_KINDS) + ")$")
    body: str = Field(min_length=3, max_length=1000)


class SettingsUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    company: str | None = Field(default=None, min_length=2, max_length=160)
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=12, max_length=256)

    @model_validator(mode="after")
    def complete_password_change(self):
        if not any((self.full_name, self.company, self.new_password)):
            raise ValueError("Choose a setting to update")
        if self.new_password and not self.current_password:
            raise ValueError("Current password is required")
        return self


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if not value or "@" not in value or value.startswith("@") or "." not in value.split("@")[-1] or any(c.isspace() for c in value):
        raise HTTPException(422, "Invalid email")
    return value


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + hashed.hex()


def verify_password(password: str, hashed: str) -> bool:
    try:
        algorithm, salt, value = hashed.split("$")
        if algorithm != "scrypt":
            return False
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(check, bytes.fromhex(value))
    except (ValueError, TypeError, MemoryError):
        return False


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue(db: AsyncSession, workspace: uuid.UUID, user: uuid.UUID | None):
    token = "iqp_" + secrets.token_urlsafe(36)
    db.add(PortalSession(token_digest=digest(token), workspace_id=workspace, user_id=user,
                         expires_at=datetime.now(timezone.utc) + timedelta(hours=8)))
    await db.commit()
    return {"token": token, "expires_in": 28800}


# Resolving a session costs two or three database round trips, which dominate read latency when the database is
# far from the API. Reads reuse the result briefly; writes always re-check, and sign-out and settings changes
# evict it, so a revoked session stops working immediately in this process and within the TTL elsewhere.
IDENTITY_CACHE_SECONDS = float(os.getenv("IDENTITY_CACHE_SECONDS", "15"))
_identity_cache: dict[str, tuple[float, PortalSession, PortalWorkspace]] = {}


async def identity(request: Request, db: AsyncSession = Depends(get_db)):
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token.startswith("iqp_"):
        raise HTTPException(401, "Sign in to continue")
    key = digest(token)
    cacheable = request.method == "GET" and IDENTITY_CACHE_SECONDS > 0
    hit = _identity_cache.get(key) if cacheable else None
    if hit and time.monotonic() - hit[0] < IDENTITY_CACHE_SECONDS and hit[1].expires_at > datetime.now(timezone.utc):
        return hit[1], hit[2]
    record = await db.get(PortalSession, key)
    if record is None or record.revoked_at or record.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired")
    workspace = await db.get(PortalWorkspace, record.workspace_id)
    if workspace is None:
        raise HTTPException(401, "Workspace unavailable")
    if workspace.kind == "customer":
        member = await db.scalar(select(PortalMembership.id).where(
            PortalMembership.user_id == record.user_id,
            PortalMembership.workspace_id == record.workspace_id,
        ))
        if not member:
            raise HTTPException(403, "Workspace access denied")
    elif workspace.kind == "showcase":
        if record.user_id is not None or record.workspace_id != SHOWCASE_ID:
            raise HTTPException(403, "Invalid showcase scope")
    else:
        raise HTTPException(403, "Unknown workspace type")
    if cacheable:
        if len(_identity_cache) > 2000:
            _identity_cache.clear()
        _identity_cache[key] = (time.monotonic(), record, workspace)
    return record, workspace


@router.post("/register", status_code=201)
async def register(payload: RegisterInput, db: AsyncSession = Depends(get_db)):
    registration_limit.check()
    if os.getenv("PUBLIC_READ_ONLY", "").lower() == "true":
        raise HTTPException(503, "Customer registration is disabled on the read-only dataset service")
    email = normalize_email(payload.email)
    if await db.scalar(select(PortalUser.id).where(PortalUser.email == email)):
        raise HTTPException(409, "Account already exists")
    user = PortalUser(email=email, full_name=payload.full_name.strip(), password_hash=password_hash(payload.password))
    workspace = PortalWorkspace(name=payload.company.strip(), kind="customer")
    db.add_all([user, workspace])
    await db.flush()
    db.add(PortalMembership(user_id=user.id, workspace_id=workspace.id, role="owner"))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Account already exists") from None
    result = await issue(db, workspace.id, user.id)
    return {**result, "workspace": workspace.name, "kind": workspace.kind}


@router.post("/login")
async def login(payload: LoginInput, db: AsyncSession = Depends(get_db)):
    email = normalize_email(payload.email)
    login_throttle.check(email)   # locked accounts are refused before any password work is done
    user = await db.scalar(select(PortalUser).where(PortalUser.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        # Do not distinguish missing accounts from invalid passwords; both count toward the lockout.
        login_throttle.record_failure(email)
        raise HTTPException(401, "Invalid email or password")
    login_throttle.record_success(email)
    membership = await db.scalar(select(PortalMembership).where(
        PortalMembership.user_id == user.id).order_by(PortalMembership.created_at))
    if membership is None:
        raise HTTPException(403, "No active workspace")
    workspace = await db.get(PortalWorkspace, membership.workspace_id)
    result = await issue(db, workspace.id, user.id)
    return {**result, "workspace": workspace.name, "kind": workspace.kind}


@router.post("/showcase")
async def showcase(db: AsyncSession = Depends(get_db)):
    if os.getenv("INVOICEIQ_PORTAL_SHOWCASE", "").lower() != "true":
        raise HTTPException(404, "Public showcase unavailable")
    workspace = await db.get(PortalWorkspace, SHOWCASE_ID)
    if not workspace or workspace.kind != "showcase":
        raise HTTPException(503, "Isolated showcase has not been provisioned")
    result = await issue(db, workspace.id, None)
    return {**result, "workspace": workspace.name, "kind": workspace.kind}


@router.get("/me")
async def me(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    membership = (await db.scalar(select(PortalMembership).where(
        PortalMembership.user_id == session.user_id,
        PortalMembership.workspace_id == workspace.id,
    ))) if session.user_id else None
    return {"workspace": workspace.name, "workspace_id": str(workspace.id),
            "kind": workspace.kind, "role": membership.role if membership else "visitor",
            "user_id": str(session.user_id) if session.user_id else None}


@router.get("/settings")
async def get_settings(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    if workspace.kind != "customer" or session.user_id is None:
        raise HTTPException(403, "Account settings require a customer session")
    user = await db.get(PortalUser, session.user_id)
    member = await db.scalar(select(PortalMembership).where(
        PortalMembership.user_id == user.id, PortalMembership.workspace_id == workspace.id))
    return {"email": user.email, "full_name": user.full_name, "company": workspace.name,
            "role": member.role, "session_expires_at": session.expires_at.isoformat()}


@router.patch("/settings")
async def update_settings(payload: SettingsUpdate, subject=Depends(identity),
                          db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    if workspace.kind != "customer" or session.user_id is None:
        raise HTTPException(403, "Account settings require a customer session")
    user = await db.get(PortalUser, session.user_id)
    member = await db.scalar(select(PortalMembership).where(
        PortalMembership.user_id == user.id, PortalMembership.workspace_id == workspace.id))
    if payload.company is not None:
        if member.role not in {"owner", "admin"}:
            raise HTTPException(403, "Workspace administrator required")
        workspace.name = payload.company.strip()
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.new_password is not None:
        if not verify_password(payload.current_password or "", user.password_hash):
            raise HTTPException(403, "Current password is incorrect")
        user.password_hash = password_hash(payload.new_password)
        await db.execute(update(PortalSession).where(
            PortalSession.user_id == user.id, PortalSession.token_digest != session.token_digest,
            PortalSession.revoked_at.is_(None),
        ).values(revoked_at=datetime.now(timezone.utc)))
    await db.commit()
    _identity_cache.clear()
    return {"ok": True}


@router.delete("/session")
async def logout(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, _ = subject
    session.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    _identity_cache.clear()
    return {"ok": True}


@router.get("/intelligence")
async def intelligence(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    _, workspace = subject
    try:
        contract = await run_in_threadpool(verified_contract)
    except PaymentRiskArtifactError as exc:
        raise HTTPException(503, "Verified model artifact unavailable") from exc
    invoices = (await db.scalars(select(PortalInvoice).where(
        PortalInvoice.workspace_id == workspace.id).order_by(
            PortalInvoice.risk_probability.desc().nullslast()).limit(2001))).all()
    if len(invoices) > 2000:
        raise HTTPException(422, "Model intelligence supports at most 2,000 invoices")
    valid = [invoice for invoice in invoices if invoice.risk_probability is not None
             and invoice.risk_provenance == provenance(contract)]
    return {"workspace": workspace.name, "kind": workspace.kind,
            "model_version": contract["version"], "artifact_sha256": contract["artifact_sha256"],
            "total": len(invoices), "scored": len(valid),
            "invoices": [{"id": str(row.id), "number": row.invoice_number,
                          "counterparty": row.counterparty,
                          "probability": str(row.risk_probability),
                          "balance": str(row.amount_total - row.amount_paid)} for row in valid[:50]]}


@router.get("/intelligence/{invoice_id}")
async def invoice_intelligence(invoice_id: uuid.UUID, subject=Depends(identity),
                               db: AsyncSession = Depends(get_db)):
    _, workspace = subject
    return await explain_invoice(db, workspace.id, invoice_id)


@router.post("/score")
async def score_customer_workspace(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    if workspace.kind != "customer" or session.user_id is None:
        raise HTTPException(403, "Only a customer administrator may score invoices")
    role = await db.scalar(select(PortalMembership.role).where(
        PortalMembership.workspace_id == workspace.id, PortalMembership.user_id == session.user_id))
    if role not in {"owner", "admin"}:
        raise HTTPException(403, "Workspace administrator required")
    result = await score_workspace(db, workspace.id)
    await db.commit()
    _bump(workspace.id)
    return result


def invoice_json(row):
    return {"id": str(row.id), "number": row.invoice_number, "counterparty": row.counterparty,
            "posting_date": str(row.posting_date), "due_date": str(row.due_date),
            "amount": str(row.amount_total), "paid": str(row.amount_paid),
            "balance": str(row.amount_total - row.amount_paid),
            "risk_probability": str(row.risk_probability) if row.risk_probability is not None else None,
            "risk_provenance": row.risk_provenance}


@router.get("/overview")
async def overview(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    return await _cached(workspace, session, "overview", None, lambda: _overview(db, session, workspace))


async def _overview(db, session, workspace):
    invoices = (await db.scalars(select(PortalInvoice).where(PortalInvoice.workspace_id == workspace.id)
                                 .order_by(PortalInvoice.posting_date.desc(), PortalInvoice.id))).all()
    today = date.today()
    invoiced = sum((r.amount_total for r in invoices), Decimal(0))
    open_balance = sum((r.amount_total - r.amount_paid for r in invoices), Decimal(0))
    overdue = sum((r.amount_total - r.amount_paid for r in invoices if r.due_date < today), Decimal(0))
    paid = sum((r.amount_paid for r in invoices), Decimal(0))
    event_scope = [PortalCaseEvent.workspace_id == workspace.id]
    if workspace.kind == "showcase":
        event_scope.append(PortalCaseEvent.actor_scope.in_((session.token_digest, SHOWCASE_BASELINE_SCOPE)))
    events = (await db.scalars(select(PortalCaseEvent).where(*event_scope)
                               .order_by(PortalCaseEvent.created_at.desc()).limit(100))).all()
    return {"workspace": workspace.name, "kind": workspace.kind,
            "provenance": "Illustrative, isolated showcase records" if workspace.kind == "showcase" else "Customer workspace",
            "metrics": {"invoiced": str(invoiced), "open": str(open_balance), "overdue": str(overdue),
                        "overdue_share": str(round(overdue / open_balance * 100, 2)) if open_balance else "0",
                        "collected": str(paid), "score_coverage": sum(r.risk_probability is not None for r in invoices),
                        "invoice_count": len(invoices)},
            "invoices": [invoice_json(x) for x in invoices[:100]],
            "total": len(invoices),
            "actions": [{"id": str(e.id), "invoice_id": str(e.invoice_id), "decision": e.decision,
                         "note": e.note, "created_at": e.created_at.isoformat()} for e in events]}


async def _records(db: AsyncSession, session, workspace):
    invoices = (await db.scalars(select(PortalInvoice).where(PortalInvoice.workspace_id == workspace.id))).all()
    scope = [PortalCaseEvent.workspace_id == workspace.id]
    if workspace.kind == "showcase":
        scope.append(PortalCaseEvent.actor_scope.in_((session.token_digest, SHOWCASE_BASELINE_SCOPE)))
    events = (await db.scalars(select(PortalCaseEvent).where(*scope))).all()
    return invoices, events


@router.get("/recovery-summary")
async def portal_recovery_summary(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject

    async def build():
        invoices, events = await _records(db, session, workspace)
        return {"workspace": workspace.name, "kind": workspace.kind, **recovery_summary(invoices, events)}
    return await _cached(workspace, session, "recovery", None, build)


class AssistantTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=2000)


class AssistantInput(BaseModel):
    question: str = Field(min_length=3, max_length=600)
    history: list[AssistantTurn] = Field(default_factory=list, max_length=6)


_ASSISTANT_CALLS: dict[str, list[float]] = {}
ASSISTANT_LIMIT_PER_MINUTE = 8


def _assistant_allowed(key: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _ASSISTANT_CALLS.get(key, []) if now - t < 60]
    if len(recent) >= ASSISTANT_LIMIT_PER_MINUTE:
        _ASSISTANT_CALLS[key] = recent
        return False
    recent.append(now)
    _ASSISTANT_CALLS[key] = recent
    if len(_ASSISTANT_CALLS) > 5000:
        _ASSISTANT_CALLS.clear()
    return True


async def _invoke_first(clients, messages, timeout):
    """Try each client once, in order; provider overload or rate limits move on immediately."""
    for client in clients:
        if client is None:
            continue
        try:
            return await asyncio.wait_for(client.ainvoke(messages), timeout=timeout)
        except Exception:
            continue
    return None


class TranscribeInput(BaseModel):
    audio: str = Field(min_length=100, max_length=1_000_000)
    mime: str = Field(pattern=r"^audio/(webm|ogg|mp4|mpeg|wav|x-m4a)$")


@router.post("/transcribe")
async def transcribe(payload: TranscribeInput, subject=Depends(identity)):
    """Speech-to-text fallback for browsers whose own recognition service is unreachable."""
    from langchain_core.messages import HumanMessage

    from app.agents.finance_agent import reply_text
    from app.agents.llm_client import _llm_chat, gemini_fallback_chats

    session, _ = subject
    if not _assistant_allowed("tx:" + session.token_digest):
        raise HTTPException(429, "Too many voice requests; wait a minute and try again")
    try:
        raw = base64.b64decode(payload.audio, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "Audio is not valid base64")
    if not 1000 <= len(raw) <= 700_000:
        raise HTTPException(422, "Recording is too short or too long")
    llm, _model = _llm_chat()
    if llm is None:
        raise HTTPException(503, "Voice transcription is not configured on the API")
    if hasattr(llm, "timeout"):
        llm.timeout = 45
    message = HumanMessage(content=[
        {"type": "text", "text": "Transcribe the speech in this audio verbatim. Reply with only the transcript, "
                                 "or an empty reply if there is no speech."},
        {"type": "media", "data": payload.audio, "mime_type": payload.mime}])
    # Fallback models first: they are the fast ones, and LLM_MODEL may be a slower reasoning model.
    fallbacks = [client for client, _ in gemini_fallback_chats(timeout=45)]
    from app.services.langsmith_tracing import untraced

    with untraced():  # recorded customer audio never goes to LangSmith
        reply = await _invoke_first([*fallbacks, llm], [message], timeout=45)
    if reply is None:
        raise HTTPException(503, "Transcription did not respond; try again shortly")
    return {"text": reply_text(reply.content)[:600]}


async def _assistant_portfolio(db: AsyncSession, session, workspace):
    """Everything the agent may read, loaded up front so a streamed answer does not need the request's session."""
    from app.agents.finance_tools import Portfolio

    invoices, events = await _records(db, session, workspace)
    payments = (await db.scalars(select(PortalPayment).where(PortalPayment.workspace_id == workspace.id))).all()
    notes = (await db.scalars(select(PortalNote).where(*_note_visibility(session, workspace))
                              .order_by(PortalNote.created_at.desc()).limit(2000))).all()

    async def explain(invoice_id):
        async with async_session_factory() as own:
            return await explain_invoice(own, workspace.id, invoice_id)

    from app.services import note_embeddings

    return Portfolio(invoices, events, payments=payments, workspace=workspace, explain=explain, notes=notes,
                     embed_query=note_embeddings.embed_query if note_embeddings.available() else None)


async def _assistant_answer(portfolio, payload: AssistantInput, workspace, on_event=None) -> dict:
    from app.agents.finance_agent import answer_question, build_providers
    from app.services.assistant_tracing import TraceRecorder

    recorder = TraceRecorder()
    result = await answer_question(portfolio, payload.question.strip(),
                                   [(t.role, t.content) for t in payload.history], build_providers(), recorder,
                                   on_event=on_event)
    await recorder.persist(async_session_factory, workspace.id)
    return {"answer": result.answer, "model": result.model, "provider": result.provider,
            "fallback_used": result.fallback_used, "tools_used": result.tools_called,
            "proposals": result.proposals, "verification": result.verification,
            "trace_id": str(recorder.run_id), "grounded_in": "workspace figures as of " + str(date.today())}


@router.post("/assistant")
async def assistant(payload: AssistantInput, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Answer with a read-only tool-calling agent; every figure is checked against what the tools returned."""
    session, workspace = subject
    if not _assistant_allowed(session.token_digest):
        raise HTTPException(429, "Too many questions; wait a minute and try again")
    portfolio = await _assistant_portfolio(db, session, workspace)
    return await _assistant_answer(portfolio, payload, workspace)


@router.post("/assistant-stream")
async def assistant_stream(payload: AssistantInput, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """The same answer as /assistant, as server-sent events: progress steps while the agent works, then the
    verified answer. The answer text is only sent once its figures and suggestions have been checked."""
    session, workspace = subject
    if not _assistant_allowed(session.token_digest):
        raise HTTPException(429, "Too many questions; wait a minute and try again")
    queue: asyncio.Queue = asyncio.Queue()

    async def produce():
        try:
            # The workspace loads on its own session while the client already sees progress.
            async with async_session_factory() as own:
                portfolio = await _assistant_portfolio(own, session, workspace)
            await queue.put({"type": "answer", **await _assistant_answer(portfolio, payload, workspace,
                                                                         on_event=queue.put_nowait)})
        except Exception:  # noqa: BLE001 - the client gets a clean error event, never a stack trace
            await queue.put({"type": "error", "detail": "The assistant could not answer; try again shortly"})
        finally:
            await queue.put(None)

    async def events():
        task = asyncio.create_task(produce())  # finishes and saves its trace even if the client leaves
        _streams.add(task)
        task.add_done_callback(_streams.discard)
        yield ": ok\n\n"
        yield "data: " + json.dumps({"type": "status", "stage": "load", "label": "Reading your workspace…"}) + "\n\n"
        while (item := await queue.get()) is not None:
            yield f"data: {json.dumps(item, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


_streams: set = set()


@router.get("/assistant-traces")
async def assistant_traces(days: int = Query(default=7, ge=1, le=90), subject=Depends(identity),
                           db: AsyncSession = Depends(get_db)):
    """Reliability of the assistant in this workspace: calls, failures and latency per step."""
    from app.services.assistant_tracing import trace_summary

    _, workspace = subject
    return await trace_summary(db, workspace.id, days)


EVAL_RESULTS = Path(__file__).resolve().parents[1] / "agents" / "eval_results.json"


@router.get("/assistant-reliability")
async def assistant_reliability(days: int = Query(default=7, ge=1, le=90), subject=Depends(identity),
                                db: AsyncSession = Depends(get_db)):
    """The reliability page: this workspace's live answers from the traces, plus the published eval results."""
    from app.services.assistant_tracing import reliability

    _, workspace = subject
    try:
        evaluation = json.loads(EVAL_RESULTS.read_text())
    except (OSError, ValueError):
        evaluation = None
    return {"live": await reliability(db, workspace.id, days), "eval": evaluation}


@router.get("/invoices")
async def list_portal_invoices(
    q: str = Query(default="", max_length=160),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="posting_desc", pattern="^(posting_desc|posting_asc|due_asc|due_desc|number_asc)$"),
    subject=Depends(identity), db: AsyncSession = Depends(get_db),
):
    _, workspace = subject
    filters = [PortalInvoice.workspace_id == workspace.id]
    if q.strip():
        term = f"%{q.strip().replace('%', r'\%').replace('_', r'\_')}%"
        filters.append(or_(PortalInvoice.invoice_number.ilike(term, escape="\\"),
                           PortalInvoice.counterparty.ilike(term, escape="\\")))
    order = {
        "posting_desc": PortalInvoice.posting_date.desc(),
        "posting_asc": PortalInvoice.posting_date.asc(),
        "due_asc": PortalInvoice.due_date.asc(),
        "due_desc": PortalInvoice.due_date.desc(),
        "number_asc": PortalInvoice.invoice_number.asc(),
    }[sort]
    total = await db.scalar(select(func.count()).select_from(PortalInvoice).where(*filters)) or 0
    rows = (await db.scalars(select(PortalInvoice).where(*filters)
                             .order_by(order, PortalInvoice.id)
                             .offset((page - 1) * page_size).limit(page_size))).all()
    return {"invoices": [invoice_json(row) for row in rows], "total": total,
            "page": page, "page_size": page_size}


@router.post("/decisions", status_code=201)
async def decision(payload: DecisionInput, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    if workspace.kind == "showcase" and os.getenv("INVOICEIQ_PORTAL_SHOWCASE", "").lower() != "true":
        raise HTTPException(403, "Showcase disabled")
    if payload.decision not in {"APPROVE", "REJECT", "ESCALATE", "RESET"}:
        raise HTTPException(422, "Unknown decision")
    row = await db.scalar(select(PortalInvoice.id).where(
        PortalInvoice.id == payload.invoice_id, PortalInvoice.workspace_id == workspace.id))
    if row is None:
        raise HTTPException(404, "Invoice not found")
    if workspace.kind == "customer":
        member = await db.scalar(select(PortalMembership).where(
            PortalMembership.user_id == session.user_id, PortalMembership.workspace_id == workspace.id))
        if member is None or member.role not in {"owner", "admin", "reviewer"}:
            raise HTTPException(403, "Approval permission required")
    db.add(PortalCaseEvent(workspace_id=workspace.id, invoice_id=payload.invoice_id,
                           actor_scope=str(session.user_id) if session.user_id else session.token_digest,
                           decision=payload.decision, note=payload.note.strip()))
    invoice = await db.get(PortalInvoice, payload.invoice_id)
    # The decision and its domain event commit together; the event drives the approval alerts.
    scope = session.token_digest if workspace.kind == "showcase" else "workspace"
    base = {"case_id": str(payload.invoice_id), "workspace_id": str(workspace.id), "visibility_scope": scope}
    if payload.decision == "ESCALATE":
        topic = event_topics.REVIEW_REQUESTED
        data = {**base, "action_type": "ESCALATION", "reason": payload.note.strip(),
                "subject": f"{invoice.invoice_number} · {invoice.counterparty} · ₹{invoice.amount_total - invoice.amount_paid:,.2f} outstanding"}
    else:
        topic, data = event_topics.REVIEW_RESOLVED, {**base, "decision": payload.decision}
    envelope = await TransactionalEventPublisher(db).publish(topic, str(payload.invoice_id), data)
    await db.commit()
    _bump(workspace.id)
    try:  # broker when reachable (the consumer applies it), otherwise in-process
        await deliver_event(async_session_factory, get_publisher(), topic, envelope)
    except Exception:  # noqa: BLE001 - the decision is already saved; the outbox row stays pending
        pass
    return {"ok": True, "mode": "isolated_showcase" if workspace.kind == "showcase" else "customer_portal",
            "external_effects": False}


def _note_visibility(session, workspace) -> list:
    """Showcase visitors see the shared sample notes plus their own; customer members see the workspace's."""
    scope = [PortalNote.workspace_id == workspace.id]
    if workspace.kind == "showcase":
        scope.append(PortalNote.author_scope.in_((session.token_digest, SHOWCASE_BASELINE_SCOPE)))
    return scope


def note_json(note, number: str | None = None) -> dict:
    return {"id": str(note.id), "invoice_id": str(note.invoice_id), "invoice": number, "kind": note.kind,
            "body": note.body, "created_at": note.created_at.isoformat() if note.created_at else None,
            "sample": note.author_scope == SHOWCASE_BASELINE_SCOPE}


@router.post("/notes", status_code=201)
async def add_note(payload: NoteInput, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    if workspace.kind == "showcase" and os.getenv("INVOICEIQ_PORTAL_SHOWCASE", "").lower() != "true":
        raise HTTPException(403, "Showcase disabled")
    body = payload.body.strip()
    if len(body) < 3:
        raise HTTPException(422, "Write at least 3 characters")
    if not _assistant_allowed("note:" + session.token_digest):
        raise HTTPException(429, "Too many notes; wait a minute and try again")
    invoice = await db.scalar(select(PortalInvoice).where(
        PortalInvoice.id == payload.invoice_id, PortalInvoice.workspace_id == workspace.id))
    if invoice is None:
        raise HTTPException(404, "Invoice not found")
    if workspace.kind == "customer":
        member = await db.scalar(select(PortalMembership).where(
            PortalMembership.user_id == session.user_id, PortalMembership.workspace_id == workspace.id))
        if member is None or member.role not in {"owner", "admin", "reviewer"}:
            raise HTTPException(403, "Reviewer permission required to add notes")
    note = PortalNote(workspace_id=workspace.id, invoice_id=invoice.id, kind=payload.kind, body=body,
                      author_scope=session.token_digest if workspace.kind == "showcase" else str(session.user_id))
    from app.services import note_embeddings

    # Embedded now for semantic search; if that fails the note still saves and is found by keywords.
    vectors = await note_embeddings.embed([body], "document", timeout=5.0) if note_embeddings.available() else None
    if vectors:
        note.embedding, note.embedding_model = note_embeddings.pack(vectors[0]), note_embeddings.model_name()
    db.add(note)
    await db.commit()
    await db.refresh(note)
    _bump(workspace.id)
    return note_json(note, invoice.invoice_number)


@router.get("/notes")
async def list_notes(invoice_id: uuid.UUID | None = Query(default=None), subject=Depends(identity),
                     db: AsyncSession = Depends(get_db)):
    """Notes for one invoice, or the workspace's latest notes, newest first."""
    session, workspace = subject
    filters = _note_visibility(session, workspace)
    if invoice_id is not None:
        filters.append(PortalNote.invoice_id == invoice_id)
    rows = (await db.execute(
        select(PortalNote, PortalInvoice.invoice_number).join(PortalInvoice, PortalInvoice.id == PortalNote.invoice_id)
        .where(*filters).order_by(PortalNote.created_at.desc(), PortalNote.id).limit(200))).all()
    return {"notes": [note_json(note, number) for note, number in rows]}


@router.get("/alerts")
async def approval_alerts(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Alerts raised by escalations; open ones first. Showcase visitors see only their own."""
    session, workspace = subject
    scope = session.token_digest if workspace.kind == "showcase" else "workspace"
    rows = (await db.execute(select(ApprovalAlert, OutboxEvent.delivery_mode).join(
        OutboxEvent, OutboxEvent.id == ApprovalAlert.event_id, isouter=True).where(
        ApprovalAlert.workspace_id == workspace.id, ApprovalAlert.visibility_scope == scope)
        .order_by(ApprovalAlert.status.asc(), ApprovalAlert.created_at.desc()).limit(50))).all()
    return {"open": sum(r.status == "OPEN" for r, _ in rows),
            "alerts": [{"id": str(r.event_id), "invoice_id": str(r.case_id), "subject": r.subject,
                        "reason": r.reason, "status": r.status, "resolved_decision": r.resolved_decision,
                        "delivered_via": mode,  # kafka when the event travelled through a broker
                        "created_at": r.created_at.isoformat() if r.created_at else None} for r, mode in rows]}


class ImportItem(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=64)
    counterparty: str = Field(min_length=1, max_length=160)
    issue_date: date
    posting_date: date
    due_date: date
    amount_total: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    amount_paid: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    payment_date: date | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.issue_date > self.posting_date or self.posting_date > self.due_date:
            raise ValueError("Issue, posting and due dates must be ordered")
        return self

    @property
    def valid_balance(self) -> bool:
        return self.amount_paid <= self.amount_total and (self.amount_paid == 0 or self.payment_date is not None) and (self.payment_date is None or self.payment_date >= self.issue_date and self.payment_date <= date.today())


class ImportBatch(BaseModel):
    invoices: list[ImportItem] = Field(min_length=1, max_length=250)


@router.post("/import", status_code=201)
async def import_invoices(batch: ImportBatch, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    if workspace.kind != "customer":
        raise HTTPException(403, "Showcase data cannot be imported")
    member = await db.scalar(select(PortalMembership).where(
        PortalMembership.user_id == session.user_id, PortalMembership.workspace_id == workspace.id))
    if member is None or member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Import permission required")
    numbers = [item.invoice_number.strip() for item in batch.invoices]
    if len(set(numbers)) != len(numbers) or not all(item.valid_balance for item in batch.invoices):
        raise HTTPException(422, "Duplicate invoice number or invalid payment balance")
    exists = await db.scalar(select(PortalInvoice.id).where(
        PortalInvoice.workspace_id == workspace.id, PortalInvoice.invoice_number.in_(numbers)).limit(1))
    if exists:
        raise HTTPException(409, "An invoice number already exists in this workspace")
    for item in batch.invoices:
        invoice = PortalInvoice(workspace_id=workspace.id, invoice_number=item.invoice_number.strip(),
                                counterparty=item.counterparty.strip(), issue_date=item.issue_date,
                                posting_date=item.posting_date, due_date=item.due_date,
                                amount_total=item.amount_total, amount_paid=item.amount_paid,
                                risk_probability=None, risk_provenance=None)
        db.add(invoice)
        await db.flush()
        if item.amount_paid > 0:
            db.add(PortalPayment(workspace_id=workspace.id, invoice_id=invoice.id,
                                 paid_on=item.payment_date, amount=item.amount_paid))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "A duplicate invoice number was imported") from None
    _bump(workspace.id)
    return {"imported": len(batch.invoices), "workspace_id": str(workspace.id)}


from typing import Annotated  # noqa: E402
from app.schemas.preserved_dashboard import DashboardSelection  # noqa: E402
from app.services.portal_dashboard import build_portal_dashboard  # noqa: E402


@router.get("/dashboard")
async def portal_dashboard(
    selection: Annotated[DashboardSelection, Query()],
    subject=Depends(identity), db: AsyncSession = Depends(get_db),
):
    session, workspace = subject
    return await _cached(workspace, session, "dashboard", selection.model_dump_json(),
                         lambda: build_portal_dashboard(db, selection, subject))


def portal_csv_cell(value) -> str:
    """Prevent formula execution when exported CSV is opened in spreadsheets."""
    s = "" if value is None else str(value)
    if s.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + s
    return s


@router.get("/export")
async def export_portal_dashboard(
    selection: Annotated[DashboardSelection, Query()],
    subject=Depends(identity), db: AsyncSession = Depends(get_db),
):
    dashboard = await build_portal_dashboard(db, selection, subject, export=True)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    fields = ("invoice_number", "company", "posting_date", "due_date",
              "amount_total", "amount_paid", "balance", "days_past_due",
              "status", "probability", "case_id")
    writer.writerow(fields)
    for invoice in dashboard.invoices:
        writer.writerow([portal_csv_cell(getattr(invoice, field)) for field in fields])
    return Response(
        content=output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="invoiceiq-invoices.csv"',
                 "Cache-Control": "no-store"},
    )
