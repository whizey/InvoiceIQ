from typing import Annotated
from datetime import date, datetime, timezone
import logging
from decimal import Decimal
from uuid import UUID, uuid5, NAMESPACE_URL

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from fastapi import Request
from app.core.invoice_company_scope import authorize_invoice_mutation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.events.outbox import TransactionalEventPublisher
from app.events import topics
from app.models import AuditLog, Company, Invoice, Payment, PaymentEvent
from app.models.enums import AuditActor, InvoiceStatus, PaymentEventType, PaymentMethod, PaymentStatus
from app.schemas.invoice import InvoiceCreate, InvoiceOut, SimulatePaymentIn, InvoiceEvidenceOut
from app.services.payment_risk_scoring import ensure_payment_risk_score

router = APIRouter(prefix="/invoices", tags=["invoices"])

logger = logging.getLogger(__name__)


@router.post("", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    payload: InvoiceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Invoice:
    authorize_invoice_mutation(request, payload.company_id, resource="company")
    company = await db.get(
        Company,
        payload.company_id,
    )

    if company is None:
        raise HTTPException(
            status_code=404,
            detail="Company not found",
        )

    duplicate = (
        await db.execute(
            select(Invoice.id).where(
                Invoice.invoice_number
                == payload.invoice_number
            )
        )
    ).scalar_one_or_none()

    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail="Invoice number already exists",
        )

    if payload.due_date < payload.posting_date:
        raise HTTPException(
            status_code=400,
            detail=(
                "due_date cannot be before "
                "posting_date"
            ),
        )

    expected_payment_term = (
        payload.due_date - payload.posting_date
    ).days

    if payload.payment_term != expected_payment_term:
        raise HTTPException(
            status_code=400,
            detail=(
                "payment_term must equal the number "
                "of calendar days between posting_date "
                f"and due_date ({expected_payment_term})"
            ),
        )

    invoice = Invoice(
        company_id=payload.company_id,
        invoice_number=payload.invoice_number,
        amount_total=payload.amount_total,
        amount_paid=Decimal("0.00"),
        currency=payload.currency,
        issue_date=payload.issue_date,
        posting_date=payload.posting_date,
        due_date=payload.due_date,
        payment_term=payload.payment_term,
        payment_method_description=(
            payload.payment_method_description
        ),
        status=InvoiceStatus.SENT,
    )

    db.add(invoice)
    await db.flush()

    try:
        await ensure_payment_risk_score(
            db,
            invoice,
        )
    except Exception:
        logger.exception(
            "Optional payment-risk scoring failed "
            "for invoice %s. Invoice creation "
            "will continue.",
            invoice.invoice_number,
        )

    await db.commit()

    stmt = (
        select(Invoice)
        .options(
            selectinload(Invoice.company)
        )
        .where(
            Invoice.id == invoice.id
        )
    )

    return (
        await db.execute(stmt)
    ).scalar_one()


@router.get("", response_model=list[InvoiceOut])
async def list_invoices(
    status_filter: InvoiceStatus | None = Query(None, alias="status"),
    company_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[Invoice]:
    stmt = select(Invoice).options(selectinload(Invoice.company)).order_by(Invoice.due_date.desc())
    if status_filter is not None:
        stmt = stmt.where(Invoice.status == status_filter)
    if company_id is not None:
        stmt = stmt.where(Invoice.company_id == company_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# Must come before /{invoice_id} — otherwise "overdue" is parsed as a UUID and 422s.
@router.get("/overdue", response_model=list[InvoiceOut])
async def list_overdue_invoices(db: AsyncSession = Depends(get_db)) -> list[Invoice]:
    stmt = (
        select(Invoice)
        .options(selectinload(Invoice.company))
        .where(Invoice.status == InvoiceStatus.OVERDUE)
        .order_by(Invoice.due_date)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(invoice_id: UUID, db: AsyncSession = Depends(get_db)) -> Invoice:
    stmt = select(Invoice).options(selectinload(Invoice.company)).where(Invoice.id == invoice_id)
    result = await db.execute(stmt)
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.post("/{invoice_id}/simulate-payment", response_model=InvoiceOut)
async def simulate_payment(
    invoice_id: UUID, request: Request, payload: SimulatePaymentIn | None = None, db: AsyncSession = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=100)] = None,
) -> Invoice:
    """Mock payment simulation — stands in for a real payment gateway webhook.

    Lets the sample close the loop on "customer actually pays": records a
    Payment, updates the invoice, and lets the next `/recovery-cases/{id}/run`
    call detect it and close the case.
    """
    # SELECT ... FOR UPDATE. Without the row lock this was a lost update:
    # two concurrent calls on a 100,000 invoice with nothing paid both read
    # amount_paid=0, both clamp their payment to the full 100,000, both
    # insert a SUCCESS Payment row, and the second UPDATE overwrites the
    # first -- leaving amount_paid=100,000 against two payment rows totalling
    # 200,000, so sum(payments) no longer reconciles with the invoice. Every
    # other mutating endpoint in the app takes a lock; this one did not.
    locked = await db.execute(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    invoice = locked.scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    authorize_invoice_mutation(request, invoice.company_id)

    from app.core.config import settings
    if settings.env.lower() in {"production", "staging", "prod"}:
        raise HTTPException(403, "Payment simulation is disabled in hosted environments")
    if invoice.status in {InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF, InvoiceStatus.DRAFT}:
        raise HTTPException(409, "Invoice is not eligible for payment simulation")

    requested_amount = str(payload.amount.normalize()) if payload and payload.amount is not None else None
    receipt_id = uuid5(NAMESPACE_URL, f"invoiceiq:simulate-payment:{invoice_id}:{idempotency_key}") if idempotency_key else None
    if receipt_id:
        receipt = await db.get(AuditLog, receipt_id)
        if receipt:
            if receipt.metadata_.get("requested_amount") != requested_amount:
                raise HTTPException(409, "Idempotency key already used with a different payment request")
            return (await db.execute(select(Invoice).options(selectinload(Invoice.company)).where(Invoice.id == invoice_id))).scalar_one()

    outstanding: Decimal = invoice.amount_total - invoice.amount_paid
    if outstanding <= 0:
        raise HTTPException(status_code=400, detail="Invoice has no outstanding balance")

    amount = payload.amount if payload and payload.amount is not None else outstanding
    amount = min(amount, outstanding)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be positive")

    now = datetime.now(timezone.utc)
    db.add(
        Payment(
            invoice_id=invoice.id,
            currency=invoice.currency,
            amount=amount,
            payment_date=now,
            method=PaymentMethod.BANK_TRANSFER,
            status=PaymentStatus.SUCCESS,
        )
    )
    invoice.amount_paid += amount
    # Recomputing status from amounts is correct here and is the documented
    # state machine: mark_overdue_invoices() explicitly flips
    # SENT/PARTIALLY_PAID invoices past their due date back to OVERDUE, so a
    # partial payment on a late invoice sets PARTIALLY_PAID and the next
    # detect sweep restores OVERDUE. The status column holds one value and
    # the sweep reconciles it.
    invoice.status = (
        InvoiceStatus.PAID if invoice.amount_paid >= invoice.amount_total else InvoiceStatus.PARTIALLY_PAID
    )
    db.add(
        PaymentEvent(
            invoice_id=invoice.id,
            event_type=PaymentEventType.PAYMENT_RECEIVED,
            payload={"amount": str(amount), "simulated": True},
            occurred_at=now,
        )
    )
    await TransactionalEventPublisher(db).publish(
        topics.PAYMENT_RECEIVED,
        str(invoice.id),
        {"invoice_id": str(invoice.id), "amount": str(amount), "simulated": True},
    )
    if receipt_id:
        db.add(AuditLog(id=receipt_id, entity_type="invoice", entity_id=invoice_id,
            event_type="SIMULATED_PAYMENT_REQUEST", actor=AuditActor.SYSTEM,
            description="Idempotent simulated payment; no payment gateway called.",
            metadata_={"requested_amount": requested_amount, "simulated": True}))
    await db.commit()

    stmt = select(Invoice).options(selectinload(Invoice.company)).where(Invoice.id == invoice_id)
    result = await db.execute(stmt)
    return result.scalar_one()


@router.get('/{invoice_id}/evidence', response_model=InvoiceEvidenceOut)
async def invoice_evidence(invoice_id: UUID, as_of: date | None = None,
                           db: AsyncSession = Depends(get_db)):
    """Read stored evidence; never score, mutate, or invent missing payment history."""
    from datetime import date, time, timedelta
    from app.models import DatasetImport
    from app.schemas.invoice import InvoiceEvidenceOut
    from app.services.persisted_predictions import valid_score
    invoice = await get_invoice(invoice_id, db)
    imported = await db.get(DatasetImport, 'public_b2b_dataset')
    trusted = bool(await db.scalar(select(valid_score()).where(Invoice.id == invoice_id)))
    reconstructable = imported is not None and invoice.invoice_number.startswith('B2B-')
    cutoff = min(as_of or imported.dataset_as_of, imported.dataset_as_of) if reconstructable else None
    if as_of and not date(1900,1,1) <= as_of <= date(2100,12,30):
        raise HTTPException(422, 'Unsupported historical date')
    records = (await db.scalars(select(Payment).where(Payment.invoice_id == invoice_id)
        .order_by(Payment.payment_date,Payment.id))).all()
    balance = None
    if cutoff is not None and invoice.posting_date <= cutoff:
        stop = datetime.combine(cutoff+timedelta(days=1),time.min,timezone.utc)
        observed = sum((p.amount for p in records if p.status == PaymentStatus.SUCCESS
            and p.currency == invoice.currency and p.payment_date < stop),Decimal(0))
        balance = Decimal(0) if invoice.status in [InvoiceStatus.CANCELLED, InvoiceStatus.WRITTEN_OFF] else max(invoice.amount_total-observed,Decimal(0))
    explanation = None
    if trusted and invoice.payment_risk_explanation_metadata:
        from app.ml.champion_serving import verified_contract
        from app.services.recommendation_evidence import verified_shap
        explanation = verified_shap(
            invoice.late_payment_probability, invoice.payment_risk_explanation_metadata,
            invoice.payment_risk_drivers, verified_contract()['metadata']['features'],
        )
    return InvoiceEvidenceOut(invoice=InvoiceOut.model_validate(invoice), trusted_prediction=trusted,
        artifact_sha256=invoice.payment_risk_artifact_sha256 if trusted else None,
        explanation=explanation,
        payments=[{'amount':p.amount,'currency':p.currency,'payment_date':p.payment_date,
            'status':p.status.value,'observable_at_cutoff':p.payment_date.date()<=cutoff if cutoff else None} for p in records],
        dataset_as_of=imported.dataset_as_of if imported else None,historical_cutoff=cutoff,historical_balance=balance,
        historical_reason='Original payment events through historical cutoff; later payments shown separately' if balance is not None else 'Not available: no reconstructable source history or invoice not yet posted at cutoff')
