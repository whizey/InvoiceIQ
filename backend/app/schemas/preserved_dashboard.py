"""Data contract for the retained reference dashboard, not a new KPI inventory."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field, model_validator


class DashboardSelection(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    as_of: date | None = None
    compare_date: date | None = None
    grain: Literal["day", "week", "month", "quarter", "year"] = "month"
    date_basis: Literal["posting", "due", "payment"] = "posting"
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    search: str = Field(default="", max_length=100)
    company_id: UUID | None = None
    status: Literal[
        "all", "open", "outstanding", "partial", "overdue", "settled", "cancelled", "written_off"
    ] = "all"
    risk: Literal["any", "low", "medium", "high", "unscored"] = "any"
    sort: Literal["dpd_desc", "amount_desc", "amount_asc", "posting_desc", "posting_asc"] = "dpd_desc"
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=12, ge=1, le=100)

    @model_validator(mode="after")
    def dates(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Start date must not exceed end date")
        for day in [self.start_date, self.end_date, self.as_of, self.compare_date]:
            if day and not date(1900, 1, 1) <= day <= date(2100, 12, 30):
                raise ValueError("Date outside supported calendar")
        return self


class RetainedMetrics(BaseModel):
    invoiced: Decimal | None
    billed: Decimal | None
    invoice_count: int
    company_count: int
    open: Decimal | None
    open_count: int | None
    overdue: Decimal | None
    overdue_count: int | None
    overdue_share: Decimal | None
    collected: Decimal
    scored_open: int | None
    partial_count: int | None
    settled_count: int | None
    cancelled_count: int | None
    written_off_count: int | None
    median_invoice: Decimal | None


class ChartPoint(BaseModel):
    period: date
    invoiced: Decimal
    collected: Decimal
    open: Decimal | None = None


class Band(BaseModel):
    key: str
    count: int
    amount: Decimal


class InvoiceRow(BaseModel):
    id: UUID
    invoice_number: str
    company_id: UUID
    company: str
    posting_date: date
    due_date: date
    amount_total: Decimal
    amount_paid: Decimal | None
    balance: Decimal | None
    days_past_due: int | None
    status: str
    probability: Decimal | None
    payment_term: int | None
    case_id: UUID | None


class PreservedDashboard(BaseModel):
    generated_at: datetime
    as_of: date
    start_date: date | None
    end_date: date | None
    timezone: str
    workspace: str
    currency: str
    currency_verified: bool
    currency_note: str
    historical_available: bool
    historical_reason: str
    metrics: RetainedMetrics
    comparison: RetainedMetrics | None = None
    series: list[ChartPoint]
    aging: list[Band]
    risk: list[Band]
    daily: list[dict]
    due: list[Band]
    companies: list[dict]
    top: list[dict]
    invoices: list[InvoiceRow]
    total: int
    page: int
    page_size: int
    chart_notes: dict[str, str]
