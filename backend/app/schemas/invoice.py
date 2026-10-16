from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

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
    payment_risk_model_version: str | None
    payment_risk_scored_at: datetime | None
    payment_risk_drivers: list[dict] | None

    @field_serializer("payment_risk_drivers")
    def summary_drivers(self, drivers):
        """Keep invoice summaries compact; retain all native SHAP drivers in storage."""
        return drivers[:5] if drivers is not None else None

    status: InvoiceStatus
    company: CompanyOut
    created_at: datetime
    updated_at: datetime


class SimulatePaymentIn(BaseModel):
    """Sample/mock payment simulation — not a real payment gateway webhook.

    amount defaults to the full outstanding balance when omitted.
    """

    amount: Decimal | None = None


class InvoiceEvidenceOut(BaseModel):
    invoice: InvoiceOut
    trusted_prediction: bool
    artifact_sha256: str | None
    explanation: dict | None
    payments: list[dict]
    dataset_as_of: date | None
    historical_cutoff: date | None
    historical_balance: Decimal | None
    historical_reason: str
