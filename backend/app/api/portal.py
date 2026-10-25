"""Isolated product portal. Never use portal identities for legacy operator routes."""
import asyncio
from collections import defaultdict
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
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.db import get_db
from app.core.login_throttle import login_throttle, registration_limit
from app.models.portal import (
    PortalWorkspace, PortalUser, PortalMembership, PortalSession, PortalInvoice, PortalCaseEvent,
    PortalPayment, PortalNote, PortalReminder, PortalZohoConnection, PortalZohoEvent,
)
from app.services import zoho_sync
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
from app.services import cash_forecast, follow_up, ledger_import, reminders
from app.services.late_reasons import customer_lateness, late_reason
from app.services.portal_recovery import SHOWCASE_BASELINE_SCOPE, recovery_summary

router = APIRouter(prefix="/portal", tags=["customer portal"])
SHOWCASE_ID = uuid.UUID("60c111be-dde9-4a54-9dcc-f881d0d0aaa1")
# The real public B2B ledger, viewed as of its last posting date (scripts/provision_public_ledger.py).
PUBLIC_LEDGER_ID = uuid.UUID("3b6f0d2e-8a41-4c7e-9d15-2f6b0c9e2016")
SEED_VERSION = "showcase-v2"  # Kaveri Industrial Supplies, app/seed/showcase_seed.py

# Read cache; any write to a workspace bumps its version, so views never go stale. The TTL only bounds memory and
# the date rollover, so it can be generous.
CACHE_TTL_SECONDS = 300
_cache: dict[tuple, tuple[float, object]] = {}
_versions: dict[uuid.UUID, int] = {}


def _bump(workspace_id: uuid.UUID) -> None:
    _versions[workspace_id] = _versions.get(workspace_id, 0) + 1


async def _cached(workspace, session, name: str, extra, build, *, shared: bool = False, ttl: float | None = None):
    """Versioned read cache. Showcase entries are per visitor (their own decisions and notes layer on top),
    except ``shared`` ones whose result is the same for every visitor, so only the first one pays the
    database round trips. ``ttl`` shortens expiry for data written outside a request (approval alerts)."""
    per_visitor = workspace.kind == "showcase" and not shared
    key = (workspace.id, _versions.get(workspace.id, 0), session.token_digest if per_visitor else "", name, extra)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < (ttl or CACHE_TTL_SECONDS):
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
        if record.user_id is not None or record.workspace_id not in {SHOWCASE_ID, PUBLIC_LEDGER_ID}:
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
async def showcase(ledger: str = Query(default="kaveri", pattern="^(kaveri|public-2016)$"),
                   db: AsyncSession = Depends(get_db)):
    """A visitor session on a public workspace: the Kaveri business (default) or the real 2016 ledger."""
    if os.getenv("INVOICEIQ_PORTAL_SHOWCASE", "").lower() != "true":
        raise HTTPException(404, "Public showcase unavailable")
    workspace = await db.get(PortalWorkspace, PUBLIC_LEDGER_ID if ledger == "public-2016" else SHOWCASE_ID)
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
            "user_id": str(session.user_id) if session.user_id else None,
            "as_of": str(workspace.as_of) if workspace.as_of else None}


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
    session, workspace = subject
    return await _cached(workspace, session, "intelligence", None, lambda: _intelligence(db, workspace), shared=True)


async def _intelligence(db, workspace):
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


def invoice_json(row, reason=None):
    return {"id": str(row.id), "number": row.invoice_number, "counterparty": row.counterparty,
            "posting_date": str(row.posting_date), "due_date": str(row.due_date),
            "amount": str(row.amount_total), "paid": str(row.amount_paid),
            "balance": str(row.amount_total - row.amount_paid),
            "risk_probability": str(row.risk_probability) if row.risk_probability is not None else None,
            "risk_provenance": row.risk_provenance, "late_reason": reason}


@router.get("/overview")
async def overview(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    return await _cached(workspace, session, "overview", None, lambda: _overview(db, session, workspace))


async def _invoice_rows(db, session, workspace) -> list:
    """Every invoice in the workspace, newest posting first, loaded once per workspace version and shared by
    every visitor and view (overview, recovery, assistant); any write bumps the version."""
    async def load():
        rows = (await db.scalars(select(PortalInvoice).where(PortalInvoice.workspace_id == workspace.id)
                                 .order_by(PortalInvoice.posting_date.desc(), PortalInvoice.id))).all()
        for row in rows:  # detached, read-only snapshots: nothing lazy-loads after this session closes
            db.expunge(row)
        return list(rows)
    return await _cached(workspace, session, "invoice-rows", None, load, shared=True)


async def _overview(db, session, workspace):
    invoices = await _invoice_rows(db, session, workspace)
    today = workspace.today()
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
            "provenance": "Shared workspace" if workspace.kind == "showcase" else "Customer workspace",
            "metrics": {"invoiced": str(invoiced), "open": str(open_balance), "overdue": str(overdue),
                        "overdue_share": str(round(overdue / open_balance * 100, 2)) if open_balance else "0",
                        "collected": str(paid), "score_coverage": sum(r.risk_probability is not None for r in invoices),
                        "invoice_count": len(invoices)},
            "invoices": [invoice_json(x) for x in invoices[:100]],
            "total": len(invoices),
            "actions": [{"id": str(e.id), "invoice_id": str(e.invoice_id), "decision": e.decision,
                         "note": e.note, "created_at": e.created_at.isoformat()} for e in events]}


async def _records(db: AsyncSession, session, workspace):
    invoices = await _invoice_rows(db, session, workspace)
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
        return {"workspace": workspace.name, "kind": workspace.kind, **recovery_summary(invoices, events, workspace.today())}
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
                     today=workspace.today(),
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
            "trace_id": str(recorder.run_id), "grounded_in": "workspace figures as of " + str(workspace.today())}


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
    session, workspace = subject
    return await _cached(workspace, session, "reliability", days, lambda: _reliability_page(db, workspace, days),
                         shared=True, ttl=30)


async def _reliability_page(db, workspace, days):
    from app.services.assistant_tracing import reliability

    try:
        evaluation = json.loads(EVAL_RESULTS.read_text())
    except (OSError, ValueError):
        evaluation = None
    from app.core.config import settings
    from app.services import langsmith_tracing, note_embeddings

    # Configuration only, never the keys: lets an operator confirm the deployed settings took effect.
    tracing = {"langsmith": langsmith_tracing.enabled(),
               "project": settings.langchain_project if langsmith_tracing.enabled() else None,
               "customer_content": "sent" if settings.langsmith_log_customer_content else "redacted",
               "tracing_flag_set": bool(settings.langchain_tracing_v2), "api_key_set": bool(settings.langchain_api_key),
               "note_search": "hybrid" if note_embeddings.available() else "keywords"}
    return {"live": await reliability(db, workspace.id, days), "eval": evaluation, "tracing": tracing}


@router.get("/invoices")
async def list_portal_invoices(
    q: str = Query(default="", max_length=160),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort: str = Query(default="posting_desc", pattern="^(posting_desc|posting_asc|due_asc|due_desc|number_asc)$"),
    status: str = Query(default="all", pattern="^(all|overdue|awaiting|escalated)$"),
    subject=Depends(identity), db: AsyncSession = Depends(get_db),
):
    """`status` narrows the list to what a page counts: overdue and unpaid, overdue with no decision yet
    (awaiting), or escalated. The last two depend on the viewer's own decisions, so they are not shared."""
    session, workspace = subject
    return await _cached(workspace, session, "invoices", (q.strip(), page, page_size, sort, status),
                         lambda: _invoice_page(db, session, workspace, q, page, page_size, sort, status),
                         shared=status in {"all", "overdue"})


async def _lateness(db, session, workspace) -> dict[str, int]:
    """Each customer's typical days late, from its paid invoices; shared by every visitor."""
    async def build():
        last = dict((await db.execute(select(PortalPayment.invoice_id, func.max(PortalPayment.paid_on))
                                      .where(PortalPayment.workspace_id == workspace.id)
                                      .group_by(PortalPayment.invoice_id))).all())
        return customer_lateness(await _invoice_rows(db, session, workspace), last)
    return await _cached(workspace, session, "lateness", None, build, shared=True)


async def _late_reasons(db, session, workspace, rows) -> dict:
    """Why each overdue row is unpaid. Only notes every viewer can see are used, so the page stays shareable."""
    today = workspace.today()
    overdue = [r for r in rows if r.amount_paid < r.amount_total and r.due_date < today]
    if not overdue:
        return {}
    shared_scope = ([PortalNote.author_scope == SHOWCASE_BASELINE_SCOPE] if workspace.kind == "showcase" else [])
    notes: dict = {}
    for note in (await db.scalars(select(PortalNote).where(
            PortalNote.workspace_id == workspace.id, PortalNote.invoice_id.in_([r.id for r in overdue]),
            *shared_scope).order_by(PortalNote.created_at.desc(), PortalNote.id))).all():
        notes.setdefault(note.invoice_id, []).append(note)
    habits = await _lateness(db, session, workspace)
    return {r.id: late_reason(r, notes.get(r.id, []), habits.get(r.counterparty), today) for r in overdue}


@router.get("/follow-ups")
async def follow_ups(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Who to chase today, ranked, with the reason and the model-versus-baseline comparison behind the ranking."""
    session, workspace = subject
    return await _cached(workspace, session, "follow-ups", None, lambda: _follow_ups(db, session, workspace),
                         shared=True)


async def _follow_ups(db, session, workspace):
    today = workspace.today()
    invoices = await _invoice_rows(db, session, workspace)
    payments = (await db.scalars(select(PortalPayment).where(PortalPayment.workspace_id == workspace.id))).all()
    settled = follow_up.settled_on(invoices, payments)
    baseline = follow_up.baseline_scores(invoices, settled, today)
    scorer = follow_up.choose_scorer(invoices, settled, baseline, today)
    habits = customer_lateness(invoices, settled)
    open_rows = [r for r in invoices if r.amount_paid < r.amount_total]
    reasons = await _late_reasons(db, session, workspace, open_rows)

    def chance(row) -> float:
        if scorer["used"] == "model" and row.risk_probability is not None:
            return float(row.risk_probability)
        return baseline[row.id]

    def item(row, days_overdue):
        balance = row.amount_total - row.amount_paid
        habit = habits.get(row.counterparty)
        reason = reasons.get(row.id)
        summary = f"{follow_up.short_rupees(balance)}, " + (
            follow_up.days(days_overdue) + " overdue" if days_overdue > 0
            else "due today" if days_overdue == 0 else "due in " + follow_up.days(-days_overdue))
        if habit is not None and habit > 0:
            summary += f", usually pays {follow_up.days(habit)} late"
        on_hold = bool(reason and reason["kind"] == "dispute")
        return {**invoice_json(row, reason), "days_overdue": days_overdue, "chance_late": round(chance(row), 3),
                "priority": 0.0 if on_hold else round(follow_up.priority(balance, chance(row), days_overdue), 2),
                "summary": summary, "on_hold": on_hold}

    overdue = [item(r, (today - r.due_date).days) for r in open_rows if r.due_date < today]
    chase = sorted((i for i in overdue if not i["on_hold"]), key=lambda i: -i["priority"])
    due_soon = sorted((item(r, (today - r.due_date).days) for r in open_rows
                       if today <= r.due_date <= today + timedelta(days=follow_up.DUE_SOON_DAYS)),
                      key=lambda i: -(float(i["balance"]) * i["chance_late"]))
    chances = {r.id: chance(r) for r in invoices}
    cash = cash_forecast.forecast(invoices, settled, payments, chances, today)
    # True only for the generated public ledger (Kaveri): its customers have a fixed habit by construction, so a
    # comparison or a forecast track record measured here is not evidence the method works on a real business.
    generated = workspace.kind == "showcase" and workspace.as_of is None
    # True for a workspace frozen at a fixed date (the real 2015 ledger): the page shouldn't say "today" or
    # "this week" when "today" is a date years in the past.
    historical = workspace.as_of is not None
    return {"as_of": str(today), "workspace": workspace.name, "generated": generated, "historical": historical,
            "scorer": scorer, "cash": cash,
            "totals": {"to_chase": len(chase), "overdue_amount": str(sum(Decimal(i["balance"]) for i in chase)),
                       "on_hold": sum(i["on_hold"] for i in overdue),
                       "due_soon_amount": str(sum(Decimal(i["balance"]) for i in due_soon))},
            "chase": chase, "on_hold": [i for i in overdue if i["on_hold"]], "due_soon": due_soon}


SORTS = {"posting_desc": (lambda r: r.posting_date, True), "posting_asc": (lambda r: r.posting_date, False),
         "due_asc": (lambda r: r.due_date, False), "due_desc": (lambda r: r.due_date, True),
         "number_asc": (lambda r: r.invoice_number, False)}


async def _invoice_page(db, session, workspace, q, page, page_size, sort, status="all"):
    """Filter, sort and page the workspace's cached rows in memory: one query for decisions (when the status
    needs them) and one for the page's notes, instead of a count and a page query per request."""
    rows = await _invoice_rows(db, session, workspace)
    today = workspace.today()
    if status != "all":
        rows = [r for r in rows if r.due_date < today and r.amount_paid < r.amount_total]
    if status in {"awaiting", "escalated"}:
        _, events = await _records(db, session, workspace)
        latest = {}
        for event in sorted(events, key=lambda e: e.created_at):
            latest[event.invoice_id] = event.decision
        if status == "awaiting":
            rows = [r for r in rows if latest.get(r.id) not in {"APPROVE", "REJECT", "ESCALATE"}]
        else:
            rows = [r for r in rows if latest.get(r.id) == "ESCALATE"]
    term = q.strip().casefold()
    if term:
        rows = [r for r in rows if term in r.invoice_number.casefold() or term in r.counterparty.casefold()]
    key, descending = SORTS[sort]
    rows = sorted(sorted(rows, key=lambda r: r.id), key=key, reverse=descending)  # ties stay in id order
    window = rows[(page - 1) * page_size:page * page_size]
    reasons = await _late_reasons(db, session, workspace, window)
    return {"invoices": [invoice_json(row, reasons.get(row.id)) for row in window], "total": len(rows),
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
    """Showcase visitors see the shared notes plus their own; customer members see the workspace's."""
    scope = [PortalNote.workspace_id == workspace.id]
    if workspace.kind == "showcase":
        scope.append(PortalNote.author_scope.in_((session.token_digest, SHOWCASE_BASELINE_SCOPE)))
    return scope


def note_json(note, number: str | None = None) -> dict:
    return {"id": str(note.id), "invoice_id": str(note.invoice_id), "invoice": number, "kind": note.kind,
            "body": note.body, "created_at": note.created_at.isoformat() if note.created_at else None,
            "shared": note.author_scope == SHOWCASE_BASELINE_SCOPE}


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
    return await _cached(workspace, session, "notes", str(invoice_id), lambda: _notes(db, session, workspace, invoice_id))


async def _notes(db, session, workspace, invoice_id):
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
    # Not cached: the event consumer (possibly another process) writes alerts after a decision, and a
    # visitor who escalates should see the alert at once.
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


class ReminderRequest(BaseModel):
    invoice_id: uuid.UUID
    channel: str = Field(pattern="^(email|whatsapp)$")
    language: str = Field(pattern="^(english|hinglish)$")


class ReminderSent(ReminderRequest):
    body: str = Field(min_length=10, max_length=2000)
    draft_source: str = Field(default="template", max_length=64)


def _actor(session, workspace) -> str:
    return session.token_digest if workspace.kind == "showcase" else str(session.user_id)


async def _reminder_case(db, session, workspace, invoice_id):
    """The invoice, its facts, and the policy verdict for contacting the customer now."""
    row = await db.scalar(select(PortalInvoice).where(PortalInvoice.id == invoice_id,
                                                      PortalInvoice.workspace_id == workspace.id))
    if row is None:
        raise HTTPException(404, "Invoice not found")
    if workspace.kind == "customer":
        member = await db.scalar(select(PortalMembership).where(
            PortalMembership.user_id == session.user_id, PortalMembership.workspace_id == workspace.id))
        if member is None or member.role not in {"owner", "admin", "reviewer"}:
            raise HTTPException(403, "Reviewer permission required to send reminders")
    reason = (await _late_reasons(db, session, workspace, [row])).get(row.id)
    scope = [PortalReminder.workspace_id == workspace.id, PortalReminder.invoice_id == row.id]
    if workspace.kind == "showcase":
        scope.append(PortalReminder.actor_scope.in_((session.token_digest, SHOWCASE_BASELINE_SCOPE)))
    last = await db.scalar(select(func.max(PortalReminder.sent_at)).where(*scope))
    verdict = reminders.policy(row, reason["kind"] if reason else None, last, workspace.now())
    return row, reminders.facts(row, workspace.name, workspace.today()), verdict


@router.post("/reminder-draft")
async def reminder_draft(payload: ReminderRequest, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Policy first; if contact is allowed, a draft that uses only the case's facts (or the template)."""
    session, workspace = subject
    if not _assistant_allowed("draft:" + session.token_digest):
        raise HTTPException(429, "Too many drafts; wait a minute and try again")
    _, case, verdict = await _reminder_case(db, session, workspace, payload.invoice_id)
    if not verdict["allowed"]:
        return {"policy": verdict, "facts": case, "draft": None}
    from app.agents.llm_client import _llm_chat, gemini_fallback_chats, groq_chat

    clients = [c for c in [_llm_chat(), *gemini_fallback_chats(timeout=25), groq_chat()] if c[0] is not None]
    result = await reminders.draft(case, payload.channel, payload.language, clients)
    return {"policy": verdict, "facts": case, "draft": result}


@router.post("/reminders", status_code=201)
async def log_reminder(payload: ReminderSent, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Record a reminder the person has sent themselves. The policy is checked again: it is the gate."""
    session, workspace = subject
    row, _, verdict = await _reminder_case(db, session, workspace, payload.invoice_id)
    if not verdict["allowed"]:
        raise HTTPException(409, verdict["blocked"])
    db.add(PortalReminder(workspace_id=workspace.id, invoice_id=row.id, actor_scope=_actor(session, workspace),
                          channel=payload.channel, language=payload.language, body=payload.body.strip(),
                          balance=row.amount_total - row.amount_paid, draft_source=payload.draft_source,
                          **({"sent_at": workspace.now()} if workspace.as_of else {})))
    await db.commit()
    _bump(workspace.id)
    return {"ok": True, "external_effects": False}


@router.get("/reminders")
async def list_reminders(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Reminders sent, newest first, each with the payment that followed it (if one did). Public-workspace
    visitors see the shared history plus their own."""
    session, workspace = subject
    scope = [PortalReminder.workspace_id == workspace.id]
    if workspace.kind == "showcase":  # the shared history plus this visitor's own
        scope.append(PortalReminder.actor_scope.in_((session.token_digest, SHOWCASE_BASELINE_SCOPE)))
    rows = (await db.execute(select(PortalReminder, PortalInvoice.invoice_number, PortalInvoice.counterparty)
                             .join(PortalInvoice, PortalInvoice.id == PortalReminder.invoice_id)
                             .where(*scope).order_by(PortalReminder.sent_at.desc()).limit(200))).all()
    paid = defaultdict(list)
    ids = list({r.invoice_id for r, _, _ in rows})
    if ids:
        for p in (await db.scalars(select(PortalPayment).where(PortalPayment.invoice_id.in_(ids)))).all():
            paid[p.invoice_id].append(p)
    items = [{"id": str(r.id), "invoice_id": str(r.invoice_id), "invoice": number, "customer": customer,
              "channel": r.channel, "language": r.language, "body": r.body, "balance": str(r.balance),
              "draft_source": r.draft_source, "sent_at": r.sent_at.isoformat(),
              "payment": reminders.outcome(r.sent_at, paid[r.invoice_id])} for r, number, customer in rows]
    settled_window = [i for i in items
                      if datetime.fromisoformat(i["sent_at"]) <= workspace.now() - timedelta(
                          days=reminders.OUTCOME_DAYS) or i["payment"]]
    return {"reminders": items, "summary": {"sent": len(items), "judged": len(settled_window),
                                            "followed_by_payment": sum(bool(i["payment"]) for i in settled_window)}}


class EraseRequest(BaseModel):
    scope: str = Field(pattern="^(data|account)$")
    confirm: str = Field(max_length=160)
    password: str = Field(default="", max_length=256)


async def erase_workspace_data(db, workspace_id) -> int:
    """Delete everything a workspace imported or produced, including copies inside event payloads and traces.
    Returns the number of invoices removed; notes, payments, decisions and reminders go with them."""
    from app.models.event_effects import CaseRiskLatest, DeadLetterEvent
    from app.services.assistant_tracing import assistant_traces

    invoice_ids = select(PortalInvoice.id).where(PortalInvoice.workspace_id == workspace_id)
    count = await db.scalar(select(func.count()).select_from(PortalInvoice).where(
        PortalInvoice.workspace_id == workspace_id)) or 0
    key = str(workspace_id)
    await db.execute(delete(CaseRiskLatest).where(CaseRiskLatest.case_id.in_(invoice_ids)))
    await db.execute(delete(ApprovalAlert).where(ApprovalAlert.workspace_id == workspace_id))
    await db.execute(delete(OutboxEvent).where(OutboxEvent.payload["data"]["workspace_id"].astext == key))
    await db.execute(delete(DeadLetterEvent).where(DeadLetterEvent.payload["data"]["workspace_id"].astext == key))
    await db.execute(delete(assistant_traces).where(assistant_traces.c.workspace_id == workspace_id))
    for model in (PortalReminder, PortalNote, PortalCaseEvent, PortalPayment, PortalInvoice, PortalZohoEvent):
        await db.execute(delete(model).where(model.workspace_id == workspace_id))
    return count


@router.post("/erase")
async def erase(payload: EraseRequest, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Owner only: delete all workspace data, or the account and workspace. Confirmed by the company name,
    and for the account also the password. Immediate and permanent."""
    session, workspace = subject
    if workspace.kind != "customer":
        raise HTTPException(403, "Only a company workspace can be deleted")
    member = await db.scalar(select(PortalMembership).where(
        PortalMembership.user_id == session.user_id, PortalMembership.workspace_id == workspace.id))
    if member is None or member.role != "owner":
        raise HTTPException(403, "Only the workspace owner can delete its data")
    if payload.confirm != workspace.name:
        raise HTTPException(422, "Type the company name exactly to confirm")
    user = await db.get(PortalUser, session.user_id)
    if payload.scope == "account" and not verify_password(payload.password, user.password_hash):
        raise HTTPException(403, "Password is incorrect")
    removed = await erase_workspace_data(db, workspace.id)
    if payload.scope == "account":
        await db.delete(workspace)  # memberships and sessions cascade
        others = await db.scalar(select(func.count()).select_from(PortalMembership).where(
            PortalMembership.user_id == user.id, PortalMembership.workspace_id != workspace.id))
        if not others:
            await db.delete(user)
    await db.commit()
    _bump(workspace.id)
    _identity_cache.clear()  # cached sessions of a deleted account must not outlive it
    return {"ok": True, "scope": payload.scope, "invoices": removed}


class LedgerFile(BaseModel):
    filename: str = Field(min_length=1, max_length=200, pattern=r"(?i)\.(csv|txt|xlsx|xlsm)$")
    content: str = Field(min_length=4, max_length=3_400_000)  # base64
    mapping: dict[str, str | None] = Field(default_factory=dict)


async def _prepared(payload: LedgerFile, db, workspace):
    """Parse and validate the file; for the public workspace nothing is saved, so its own invoices don't count
    as already imported."""
    try:
        content = base64.b64decode(payload.content, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "The file could not be read") from None
    existing = set() if workspace.kind == "showcase" else {n.casefold() for n in (await db.scalars(
        select(PortalInvoice.invoice_number).where(PortalInvoice.workspace_id == workspace.id))).all()}
    try:
        return await run_in_threadpool(ledger_import.prepare, payload.filename, content, payload.mapping,
                                       existing=existing)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception:  # noqa: BLE001 - a malformed spreadsheet must not surface as a server error
        raise HTTPException(422, "The file could not be read as CSV or Excel") from None


async def _require_importer(db, session, workspace):
    if workspace.kind != "customer":
        raise HTTPException(403, "Import into your own company workspace")
    member = await db.scalar(select(PortalMembership).where(
        PortalMembership.user_id == session.user_id, PortalMembership.workspace_id == workspace.id))
    if member is None or member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Import permission required")


DEMO_TIMELINE_MAX = 300  # a bounded, predictable size for the client-side playback, not the whole year's history


@router.get("/demo-timeline")
async def demo_timeline(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """A chronological replay of this showcase workspace's own generated history -- invoices created and
    payments received, in the order they actually happened -- for the interactive "watch it happen" demo.
    Customer workspaces have no synthetic history to replay, so this is showcase-only. The invoices and
    payments here already went through the real import and scoring pipeline when the workspace was seeded;
    this endpoint only orders and replays what's already in the database, it invents nothing."""
    session, workspace = subject
    if workspace.kind != "showcase":
        raise HTTPException(403, "The demo timeline is only available on a showcase workspace")
    invoice_rows = (await db.execute(select(
        PortalInvoice.invoice_number, PortalInvoice.counterparty, PortalInvoice.issue_date,
        PortalInvoice.due_date, PortalInvoice.amount_total
    ).where(PortalInvoice.workspace_id == workspace.id))).all()
    payment_rows = (await db.execute(select(
        PortalPayment.paid_on, PortalPayment.amount, PortalInvoice.invoice_number, PortalInvoice.counterparty
    ).join(PortalInvoice, PortalInvoice.id == PortalPayment.invoice_id)
        .where(PortalPayment.workspace_id == workspace.id))).all()
    events = [{"at": str(row.issue_date), "kind": "invoice", "invoice": row.invoice_number,
              "counterparty": row.counterparty, "amount": str(row.amount_total),
              "due_date": str(row.due_date)} for row in invoice_rows]
    events += [{"at": str(row.paid_on), "kind": "payment", "invoice": row.invoice_number,
               "counterparty": row.counterparty, "amount": str(row.amount)} for row in payment_rows]
    events.sort(key=lambda e: e["at"])
    return {"workspace": workspace.name, "generated": workspace.as_of is None,
            "total": len(events), "events": events[-DEMO_TIMELINE_MAX:]}


@router.get("/example-export")
async def example_export(format: str = Query(default="zoho", pattern="^(zoho|tally)$"), subject=Depends(identity)):
    """The public business as a Zoho Books or Tally export, to try the importer with a real file layout."""
    from app.seed import showcase_seed

    today = date.today()
    ledger = showcase_seed.build(today)
    if format == "zoho":
        return {"filename": "kaveri-zoho-books-invoices.csv", "content": showcase_seed.zoho_export(ledger, today)}
    return {"filename": "kaveri-tally-bills-receivable.csv", "content": showcase_seed.tally_export(ledger, today)}


@router.post("/import-preview")
async def import_preview(payload: LedgerFile, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """What a Tally or Zoho Books export would import: detected columns, rows ready, rows skipped and why.
    Read-only, so visitors to the public workspace can try it with their own file."""
    session, workspace = subject
    if workspace.kind == "customer":
        await _require_importer(db, session, workspace)
    return (await _prepared(payload, db, workspace)).report()


@router.post("/import-file", status_code=201)
async def import_file(payload: LedgerFile, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Import the rows the preview showed as ready, then score the workspace."""
    session, workspace = subject
    await _require_importer(db, session, workspace)
    prepared = await _prepared(payload, db, workspace)
    if not prepared.rows:
        raise HTTPException(422, "No rows are ready to import")
    for row in prepared.rows:
        invoice = PortalInvoice(workspace_id=workspace.id, invoice_number=row["invoice_number"],
                                counterparty=row["counterparty"], issue_date=row["issue_date"],
                                posting_date=row["posting_date"], due_date=row["due_date"],
                                amount_total=row["amount_total"], amount_paid=row["amount_paid"])
        db.add(invoice)
        await db.flush()
        if row["amount_paid"] > 0:
            db.add(PortalPayment(workspace_id=workspace.id, invoice_id=invoice.id, paid_on=row["payment_date"],
                                 amount=row["amount_paid"]))
    try:
        await db.flush()
        scored = (await score_workspace(db, workspace.id))["scored"]
    except HTTPException:
        scored = 0  # imported anyway; scoring can be re-run from the model page
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "An invoice number was imported twice") from None
    _bump(workspace.id)
    report = prepared.report()
    return {**report, "imported": len(prepared.rows), "scored": scored}


OAUTH_STATE_MAX_AGE = timedelta(minutes=10)


@router.get("/zoho-status")
async def zoho_status(request: Request, subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    await _require_importer(db, session, workspace)
    connection = await db.scalar(select(PortalZohoConnection).where(
        PortalZohoConnection.workspace_id == workspace.id))
    connected = bool(connection and connection.webhook_secret)
    webhook_url = None
    if connected:
        # uvicorn isn't started with --proxy-headers, so request.base_url reports the scheme of the Space's
        # internal (plain HTTP) proxy hop, not the real public HTTPS one -- read it from the header instead.
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        host = request.headers.get("host", request.url.netloc)
        webhook_url = f"{scheme}://{host}/portal/zoho-webhook/{connection.webhook_secret}"
    events = (await db.scalars(select(PortalZohoEvent).where(PortalZohoEvent.workspace_id == workspace.id)
                               .order_by(PortalZohoEvent.created_at.desc()).limit(20))).all()
    return {"configured": zoho_sync.configured(), "connected": connected,
            "connected_at": connection.connected_at.isoformat() if connected and connection.connected_at else None,
            "last_synced_at": connection.last_synced_at.isoformat()
                if connection and connection.last_synced_at else None,
            "invoices_synced": connection.invoices_synced if connection else 0, "webhook_url": webhook_url,
            "api_domain": connection.api_domain if connected else None,
            "events": [{"invoice_number": e.invoice_number, "outcome": e.outcome, "reason": e.reason,
                       "received_at": e.created_at.isoformat()} for e in events]}


@router.post("/zoho-connect")
async def zoho_connect(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """Starts the OAuth handshake: a one-time `state` the callback will use to find this connection again,
    since Zoho redirects the browser straight back to the API, not through the signed-in session."""
    session, workspace = subject
    await _require_importer(db, session, workspace)
    if not zoho_sync.configured():
        raise HTTPException(503, "Zoho sync is not configured on this server")
    connection = await db.scalar(select(PortalZohoConnection).where(
        PortalZohoConnection.workspace_id == workspace.id))
    if connection is None:
        connection = PortalZohoConnection(workspace_id=workspace.id)
        db.add(connection)
    connection.oauth_state = secrets.token_urlsafe(24)
    connection.oauth_state_created_at = datetime.now(timezone.utc)
    await db.commit()
    return {"authorize_url": zoho_sync.authorize_url(connection.oauth_state)}


@router.get("/zoho-callback")
async def zoho_callback(code: str = Query(default=""), state: str = Query(default=""),
                        accounts_server: str = Query(default="", alias="accounts-server"),
                        db: AsyncSession = Depends(get_db)):
    """Public: Zoho sends the browser here after the user authorizes or declines. No session cookie survives
    this redirect (it lands on the API's own origin), so the pending connection is found by `state` instead.
    `accounts-server` is Zoho's own report of which data centre issued the code (an account on any data
    centre authorizes through accounts.zoho.com, but the code is only redeemable at its own data centre's
    token endpoint) -- using it instead of a fixed domain is what makes this work for any Zoho account."""
    from app.core.config import settings

    frontend = settings.frontend_public_url.rstrip("/") + "/settings"
    connection = (await db.scalar(select(PortalZohoConnection).where(
        PortalZohoConnection.oauth_state == state))) if state else None
    if connection is None or connection.oauth_state_created_at is None or (
            datetime.now(timezone.utc) - connection.oauth_state_created_at > OAUTH_STATE_MAX_AGE):
        return RedirectResponse(f"{frontend}?zoho=expired")
    if not code:
        return RedirectResponse(f"{frontend}?zoho=denied")
    try:
        tokens = await zoho_sync.exchange_code(code, accounts_server or None)
    except ValueError as exc:
        from urllib.parse import quote

        return RedirectResponse(f"{frontend}?zoho=error&reason={quote(str(exc)[:200])}")
    connection.access_token = tokens.get("access_token")
    connection.refresh_token = tokens.get("refresh_token") or connection.refresh_token
    expires_in = tokens.get("expires_in")
    connection.token_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                                   if expires_in else None)
    connection.api_domain = tokens.get("api_domain") or connection.api_domain
    connection.accounts_server = (accounts_server or settings.zoho_accounts_domain).rstrip("/")
    # Keep the existing secret across a re-authorization (refreshing tokens, or clicking Connect again
    # without disconnecting first) -- only an explicit Disconnect should ever invalidate a webhook URL
    # already saved in Zoho Books.
    connection.webhook_secret = connection.webhook_secret or secrets.token_urlsafe(24)
    connection.connected_at = datetime.now(timezone.utc)
    if not connection.zoho_organization_id and connection.api_domain:
        try:
            orgs = await zoho_sync.list_organizations(connection.access_token, connection.api_domain)
            default_org = next((o for o in orgs if o.get("is_default_org")), orgs[0] if orgs else None)
            if default_org:
                connection.zoho_organization_id = str(default_org.get("organization_id") or "") or None
        except ValueError:
            pass  # non-fatal here: the webhook still works; Sync Now will look it up again when needed
    connection.oauth_state = None
    connection.oauth_state_created_at = None
    await db.commit()
    return RedirectResponse(f"{frontend}?zoho=connected")


@router.post("/zoho-disconnect")
async def zoho_disconnect(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, workspace = subject
    await _require_importer(db, session, workspace)
    connection = await db.scalar(select(PortalZohoConnection).where(
        PortalZohoConnection.workspace_id == workspace.id))
    if connection:
        await db.delete(connection)
        await db.commit()
        _bump(workspace.id)
    return {"ok": True}


@router.post("/zoho-sync-now")
async def zoho_sync_now(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    """A one-time pull of invoices that already existed in Zoho before the webhook was connected -- the
    webhook only ever fires on a future create or update, so it can never retroactively deliver these."""
    from app.core.config import settings

    session, workspace = subject
    await _require_importer(db, session, workspace)
    connection = await db.scalar(select(PortalZohoConnection).where(
        PortalZohoConnection.workspace_id == workspace.id))
    if connection is None or not connection.access_token:
        raise HTTPException(409, "Connect Zoho Books first")
    if not connection.api_domain:
        # A connection made before data-centre auto-detection existed never got this populated. Every Zoho
        # API call below needs it, so surface a clear, actionable error instead of crashing on a None domain.
        raise HTTPException(409, "This connection predates automatic data-centre detection -- "
                                 "disconnect and reconnect Zoho Books to refresh it.")
    if connection.token_expires_at and connection.token_expires_at <= datetime.now(timezone.utc):
        if not connection.refresh_token:
            raise HTTPException(409, "The Zoho connection expired; disconnect and reconnect")
        try:
            tokens = await zoho_sync.refresh_access_token(
                connection.refresh_token, connection.accounts_server or settings.zoho_accounts_domain)
        except ValueError as exc:
            raise HTTPException(502, f"Could not refresh the Zoho connection: {exc}") from None
        connection.access_token = tokens.get("access_token")
        connection.api_domain = tokens.get("api_domain") or connection.api_domain
        connection.token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=tokens.get("expires_in", 3600))
        await db.commit()
    if not connection.zoho_organization_id:
        try:
            orgs = await zoho_sync.list_organizations(connection.access_token, connection.api_domain)
        except ValueError as exc:
            raise HTTPException(502, f"Could not list Zoho organizations: {exc}") from None
        default_org = next((o for o in orgs if o.get("is_default_org")), orgs[0] if orgs else None)
        if default_org is None:
            raise HTTPException(502, "No Zoho organization found on this account")
        connection.zoho_organization_id = str(default_org.get("organization_id") or "") or None
        await db.commit()
    # Fetched concurrently -- each is its own slow round trip to Zoho, and the DB-side matching that needs
    # them in order (payments need invoices upserted first) only starts once both have actually arrived.
    invoices_result, payments_result = await asyncio.gather(
        zoho_sync.list_invoices(connection.access_token, connection.api_domain, connection.zoho_organization_id),
        zoho_sync.list_payments(connection.access_token, connection.api_domain, connection.zoho_organization_id),
        return_exceptions=True)
    if isinstance(invoices_result, BaseException):
        raise HTTPException(502, f"Could not list Zoho invoices: {invoices_result}") from None
    raw_invoices = invoices_result
    imported = skipped = 0
    for raw in raw_invoices:
        try:
            mapped = zoho_sync.map_invoice({"invoice": raw})
        except ValueError:
            skipped += 1
            continue
        await zoho_sync.upsert_invoice(db, workspace.id, mapped)
        imported += 1
    # A payments failure is reported, not fatal -- it must not discard an invoice backfill that succeeded.
    payments_found = payments_applied = payments_skipped = 0
    payments_error = str(payments_result) if isinstance(payments_result, BaseException) else None
    if payments_error is None:
        raw_payments = payments_result
        payments_found = len(raw_payments)
        for raw in raw_payments:
            try:
                mapped_payment = zoho_sync.map_backfilled_payment(raw)
            except ValueError:
                payments_skipped += 1
                continue
            if await zoho_sync.upsert_backfilled_payment(db, workspace.id, mapped_payment):
                payments_applied += 1
            else:
                payments_skipped += 1
    connection.last_synced_at = datetime.now(timezone.utc)
    connection.invoices_synced += imported
    try:
        scored = (await score_workspace(db, workspace.id))["scored"]
    except HTTPException:
        scored = 0
    await db.commit()
    _bump(workspace.id)
    return {"ok": True, "found": len(raw_invoices), "imported": imported, "skipped": skipped,
            "payments_found": payments_found, "payments_applied": payments_applied,
            "payments_skipped": payments_skipped, "payments_error": payments_error, "scored": scored}


@router.post("/zoho-webhook/{secret}")
async def zoho_webhook(secret: str, payload: dict, db: AsyncSession = Depends(get_db)):
    """Public: Zoho posts here on every invoice or payment create/update once connected -- two separate
    webhooks in Zoho Books, one per module, can point at this same URL; the payload's own shape says which.
    The secret in the URL is the only authenticity check available -- Zoho's classic Books webhooks carry no
    signature in the body or headers."""
    connection = await db.scalar(select(PortalZohoConnection).where(PortalZohoConnection.webhook_secret == secret))
    if connection is None:
        raise HTTPException(404, "Unknown webhook")
    workspace = await db.get(PortalWorkspace, connection.workspace_id)
    if workspace is None:
        raise HTTPException(404, "Workspace not found")
    if isinstance(payload, dict) and "payment" in payload:
        return await _zoho_webhook_payment(db, workspace, connection, payload)
    return await _zoho_webhook_invoice(db, workspace, connection, payload)


async def _zoho_webhook_invoice(db, workspace, connection, payload):
    try:
        mapped = zoho_sync.map_invoice(payload)
    except ValueError as exc:
        number = str((payload.get("invoice") or {}).get("invoice_number") or "") or None
        db.add(PortalZohoEvent(workspace_id=workspace.id, invoice_number=number, outcome="skipped",
                               reason=str(exc)[:300]))
        await db.commit()
        return {"ok": False, "skipped": str(exc)}  # 200: a payload we understood and rejected, not a server error
    invoice, created, paid = await zoho_sync.upsert_invoice(db, workspace.id, mapped)
    db.add(PortalZohoEvent(workspace_id=workspace.id, invoice_number=mapped["invoice_number"],
                           outcome="created" if created else "updated"))
    connection.last_synced_at = datetime.now(timezone.utc)
    connection.invoices_synced += 1
    envelope = await TransactionalEventPublisher(db).publish(
        event_topics.INVOICE_SYNCED, str(invoice.id),
        {"workspace_id": str(workspace.id), "invoice_id": str(invoice.id), "created": created, "paid": paid})
    try:
        scored = (await score_workspace(db, workspace.id))["scored"]
    except HTTPException:
        scored = 0
    await db.commit()
    _bump(workspace.id)
    try:  # broker when reachable (the consumer applies it), otherwise in-process
        await deliver_event(async_session_factory, get_publisher(), event_topics.INVOICE_SYNCED, envelope)
    except Exception:  # noqa: BLE001 - the invoice is already saved; the outbox row stays pending
        pass
    return {"ok": True, "created": created, "paid": paid, "scored": scored}


async def _zoho_webhook_payment(db, workspace, connection, payload):
    try:
        mapped = zoho_sync.map_payment(payload)
    except ValueError as exc:
        db.add(PortalZohoEvent(workspace_id=workspace.id, invoice_number=None, outcome="skipped",
                               reason=str(exc)[:300]))
        await db.commit()
        return {"ok": False, "skipped": str(exc)}
    result = await zoho_sync.upsert_payment(db, workspace.id, mapped)
    first = await db.scalar(select(PortalInvoice.invoice_number).where(
        PortalInvoice.workspace_id == workspace.id,
        PortalInvoice.zoho_invoice_id == mapped["allocations"][0]["zoho_invoice_id"]))
    db.add(PortalZohoEvent(workspace_id=workspace.id, invoice_number=first, outcome="payment",
                           reason=None if not result["unmatched"] else
                           f"{result['unmatched']} of {len(mapped['allocations'])} allocations unmatched"))
    connection.last_synced_at = datetime.now(timezone.utc)
    try:
        scored = (await score_workspace(db, workspace.id))["scored"] if result["applied"] else 0
    except HTTPException:
        scored = 0
    await db.commit()
    _bump(workspace.id)
    return {"ok": True, "applied": result["applied"], "unmatched": result["unmatched"], "scored": scored}


from typing import Annotated  # noqa: E402
from app.schemas.preserved_dashboard import DashboardSelection  # noqa: E402
from app.services.portal_dashboard import build_portal_dashboard  # noqa: E402


@router.get("/dashboard")
async def portal_dashboard(
    selection: Annotated[DashboardSelection, Query()],
    subject=Depends(identity), db: AsyncSession = Depends(get_db),
):
    session, workspace = subject
    # Visitor decisions only matter to the "has a case" filter; otherwise every visitor sees the same figures.
    return await _cached(workspace, session, "dashboard", selection.model_dump_json(),
                         lambda: build_portal_dashboard(db, selection, subject),
                         shared=selection.case_presence == "any")


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
