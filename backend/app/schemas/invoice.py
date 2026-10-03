from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import InvoiceStatus
from app.schemas.company import CompanyOut


class InvoiceCreate(BaseModel):
    company_id: UUID
    invoice_number: str
    amount_total: Decimal = Field(gt=0)
    currency: str = "INR"

    issue_date: date
    posting_date: date
    due_date: date

    payment_term: int = Field(ge=0)
    payment_method_description: str


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_number: str
    amount_total: Decimal
    amount_paid: Decimal
    currency: str
    issue_date: date
    posting_date: date | None
    due_date: date
    payment_term: int | None
    payment_method_description: str | None

    late_payment_probability: Decimal | None
    payment_risk_model: str | None
    payment_risk_scored_at: datetime | None
    payment_risk_drivers: list[dict] | None

    status: InvoiceStatus
    company: CompanyOut
    created_at: datetime
    updated_at: datetime


class SimulatePaymentIn(BaseModel):
    """Demo/mock payment simulation — not a real payment gateway webhook.

    amount defaults to the full outstanding balance when omitted.
    """

    amount: Decimal | None = None
