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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.portal import (
    PortalWorkspace, PortalUser, PortalMembership, PortalSession, PortalInvoice, PortalCaseEvent,
    PortalPayment,
)

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
    return {"workspace": workspace.name, "workspace_id": str(workspace.id),
            "kind": workspace.kind, "role": "visitor" if workspace.kind == "showcase" else "member",
            "user_id": str(session.user_id) if session.user_id else None}


@router.delete("/session")
async def logout(subject=Depends(identity), db: AsyncSession = Depends(get_db)):
    session, _ = subject
    session.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}


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
    events = (await db.scalars(select(PortalCaseEvent).where(
        PortalCaseEvent.workspace_id == workspace.id,
        PortalCaseEvent.actor_scope == (str(session.user_id) if session.user_id else session.token_digest),
    ).order_by(PortalCaseEvent.created_at.desc()).limit(100))).all()
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
from fastapi import Query  # noqa: E402
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
