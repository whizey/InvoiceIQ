from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.db import get_db
from app.events import get_publisher
from app.events import topics
from app.models import Company, Invoice, Payment, PaymentEvent
from app.models.enums import InvoiceStatus, PaymentEventType, PaymentMethod, PaymentStatus
from app.schemas.invoice import InvoiceCreate, InvoiceOut, SimulatePaymentIn
from app.services.payment_risk_scoring import ensure_payment_risk_score

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    payload: InvoiceCreate,
    db: AsyncSession = Depends(get_db),
) -> Invoice:
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

    await ensure_payment_risk_score(
        db,
        invoice,
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
    limit: int = Query(50, le=200),
    offset: int = 0,
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
    invoice_id: UUID, payload: SimulatePaymentIn | None = None, db: AsyncSession = Depends(get_db)
) -> Invoice:
    """Mock payment simulation — stands in for a real payment gateway webhook.

    Lets the demo close the loop on "customer actually pays": records a
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
    await db.commit()

    await get_publisher().publish(
        topics.PAYMENT_RECEIVED,
        str(invoice.id),
        {"invoice_id": str(invoice.id), "amount": str(amount), "simulated": True},
    )

    stmt = select(Invoice).options(selectinload(Invoice.company)).where(Invoice.id == invoice_id)
    result = await db.execute(stmt)
    return result.scalar_one()
