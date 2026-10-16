"""Isolated product portal. Never use portal identities for legacy operator routes."""
import csv
import hashlib
import io
import hmac
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.db import get_db
from app.models.portal import (
    PortalWorkspace, PortalUser, PortalMembership, PortalSession, PortalInvoice, PortalCaseEvent,
    PortalPayment,
)
from app.ml.champion_serving import verified_contract
from app.ml.payment_risk_model import PaymentRiskArtifactError
from app.services.portal_risk import explain_invoice, provenance, score_workspace

router = APIRouter(prefix="/portal", tags=["customer portal"])
SHOWCASE_ID = uuid.UUID("60c111be-dde9-4a54-9dcc-f881d0d0aaa1")
SEED_VERSION = "showcase-v1"


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


async def identity(request: Request, db: AsyncSession = Depends(get_db)):
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token.startswith("iqp_"):
        raise HTTPException(401, "Sign in to continue")
    record = await db.get(PortalSession, digest(token))
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
    return record, workspace


@router.post("/register", status_code=201)
async def register(payload: RegisterInput, db: AsyncSession = Depends(get_db)):
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
    user = await db.scalar(select(PortalUser).where(PortalUser.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        # Do not distinguish missing accounts from invalid passwords.
        raise HTTPException(401, "Invalid email or password")
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
    return {"ok": True}


@router.delete("/session")
async def logout(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, _ = subject
    session.revoked_at = datetime.now(timezone.utc)
    await db.commit()
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
    invoices = (await db.scalars(select(PortalInvoice).where(PortalInvoice.workspace_id == workspace.id)
                                 .order_by(PortalInvoice.posting_date.desc(), PortalInvoice.id))).all()
    today = date.today()
    invoiced = sum((r.amount_total for r in invoices), Decimal(0))
    open_balance = sum((r.amount_total - r.amount_paid for r in invoices), Decimal(0))
    overdue = sum((r.amount_total - r.amount_paid for r in invoices if r.due_date < today), Decimal(0))
    paid = sum((r.amount_paid for r in invoices), Decimal(0))
    event_scope = [PortalCaseEvent.workspace_id == workspace.id]
    if workspace.kind == "showcase":
        event_scope.append(PortalCaseEvent.actor_scope == session.token_digest)
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
    await db.commit()
    return {"ok": True, "mode": "isolated_showcase" if workspace.kind == "showcase" else "customer_portal",
            "external_effects": False}


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
    return {"imported": len(batch.invoices), "workspace_id": str(workspace.id)}


from typing import Annotated  # noqa: E402
from app.schemas.preserved_dashboard import DashboardSelection  # noqa: E402
from app.services.portal_dashboard import build_portal_dashboard  # noqa: E402


@router.get("/dashboard")
async def portal_dashboard(
    selection: Annotated[DashboardSelection, Query()],
    subject=Depends(identity), db: AsyncSession = Depends(get_db),
):
    return await build_portal_dashboard(db, selection, subject)


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
