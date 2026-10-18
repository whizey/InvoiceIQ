from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo
from uuid import UUID

from pydantic import ConfigDict, BaseModel, Field, model_validator
from app.models.enums import InvoiceStatus, RecoveryCaseStatus
from app.schemas.invoice import InvoiceOut
from app.schemas.recovery import RecoveryCaseListItemOut


class AnalyticsRange(BaseModel):
    start_date: date
    end_date: date
    source_timezone: str | None = Field(default=None, exclude=True)
    grain: Literal["month", "quarter", "year"] = "month"
    workflow_date_axis: Literal["event", "invoice"] = "event"
    case_id: UUID | None = None
    approval_status: Literal["PENDING_APPROVAL", "REVIEW_APPROVED", "REVIEW_REJECTED"] | None = None
    policy_reason: str = Field(default="", max_length=100)
    action: str = Field(default="", max_length=60)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=25, ge=1, le=100)
    search: str = Field(default="", max_length=100)
    invoice_status: InvoiceStatus | None = None
    case_status: RecoveryCaseStatus | None = None
    risk: Literal["LOW", "MEDIUM", "HIGH", "UNSCORED"] | None = None
    sort: Literal["date_desc", "date_asc", "due_desc", "due_asc", "amount_desc", "amount_asc"] = "date_desc"
    view: Literal["overview", "invoices", "cases", "investigations", "approvals", "ml"] = "overview"

    @model_validator(mode="after")
    def valid_interval(self):
        if self.start_date > self.end_date:
            raise ValueError("Start date must be on or before end date")
        if self.start_date < date(1900, 1, 1) or self.end_date > date(2100, 12, 30):
            raise ValueError("Supported dates are 1900-01-01 through 2100-12-30")
        if (self.end_date - self.start_date).days > 3660:
            raise ValueError("Choose an interval of at most ten years")
        return self

    def timestamps(self, business_timezone: str) -> tuple[datetime, datetime]:
        zone = ZoneInfo(business_timezone)
        return (datetime.combine(self.start_date, time.min, zone).astimezone(timezone.utc),
                datetime.combine(self.end_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc))


class AnalyticsSummary(BaseModel):
    invoice_count: int = 0
    total_invoice_amount: Decimal = Decimal(0)
    outstanding_amount: Decimal = Decimal(0)
    overdue_amount: Decimal = Decimal(0)
    overdue_invoice_count: int = 0
    paid_invoice_count: int = 0
    active_cases: int = 0
    case_count: int = 0
    recovered_amount: Decimal = Decimal(0)
    high_risk_invoices: int = 0
    predictions: int = 0
    investigations: int = 0
    actions: int = 0
    policy_overrides: int = 0
    pending_approvals: int = 0
    approved_approvals: int = 0
    rejected_approvals: int = 0
    completed_investigations: int = 0
    recommendations: int = 0
    average_review_hours: float | None = None


class AnalyticsBucket(BaseModel):
    month: str
    invoice_count: int
    invoice_amount: Decimal
    recovered_amount: Decimal


class ActivityRecord(BaseModel):
    id: str
    case_id: str
    occurred_at: datetime
    kind: Literal["investigation", "action", "approval", "prediction"]
    status: str
    model_version: str | None = None
    action: str | None = None
    recommendation: str | None = None
    reason: str | None = None
    provenance: dict | None = None
    simulated: bool | None = None
    tool_status: Literal["completed", "unavailable", "not_recorded"] | None = None


class HistoricalSnapshot(BaseModel):
    available: bool = False
    coverage_started_at: datetime | None = None
    effective_as_of: datetime
    invoice_count: int | None = None
    outstanding_amount: Decimal | None = None
    overdue_amount: Decimal | None = None
    overdue_invoice_count: int | None = None
    active_cases: int | None = None
    case_status_distribution: dict[str, int] = Field(default_factory=dict)
    reason: str


class InferenceTelemetry(BaseModel):
    requests: int = 0
    succeeded: int = 0
    errors: int = 0
    in_flight: int = 0
    average_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    coverage_started_at: datetime | None = None
    complete_interval: bool = False
    model_versions: dict[str, int] = Field(default_factory=dict)


class InferenceRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    invoice_id: str | None
    model_kind: Literal["b2b", "bpi"]
    model_name: str
    model_version: str | None
    source: str
    started_at: datetime
    completed_at: datetime | None
    latency_ms: float | None
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    error_code: str | None
    probability: float | None


class FinancialCohortMetrics(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    reconstructable: bool = False
    reason: str
    cutoff: date
    observed_invoice_count: int = 0
    outstanding_amount: Decimal | None = None
    overdue_amount: Decimal | None = None
    overdue_invoice_count: int | None = None
    resolved_invoice_count: int = 0
    late_invoice_count: int = 0
    actual_late_rate: float | None = None
    average_delay_days: float | None = None
    median_delay_days: float | None = None
    mean_predicted_probability: float | None = None
    aging: list[dict] = Field(default_factory=list)
    delay_series: list[dict] = Field(default_factory=list)
    shap_importance: list[dict] = Field(default_factory=list)
    shap_invoice_count: int = 0


class AnalyticsOut(BaseModel):
    reconciliation: dict[str, object] | None = None
    dataset_as_of: date | None = None
    grain: Literal["month", "quarter", "year"] = "month"
    workflow_date_axis: Literal["event", "invoice"] = "event"
    financial: FinancialCohortMetrics | None = None
    metric_definitions: dict[str, dict] = Field(default_factory=dict)
    stored_predictions: int = 0
    persisted_prediction_provenance: dict = Field(default_factory=dict)
    start_date: date
    end_date: date
    business_timezone: str
    currency: str
    generated_at: datetime
    workspace_id: str
    summary: AnalyticsSummary
    changes_percent: dict[str, Decimal | None]
    previous: AnalyticsSummary
    previous_start_date: date
    previous_end_date: date
    series: list[AnalyticsBucket]
    risk_distribution: dict[str, int]
    action_distribution: dict[str, int]
    case_status_distribution: dict[str, int]
    prediction_outcomes: dict[str, int]
    tool_status_distribution: dict[str, int]
    model_versions: dict[str, int]
    historical_invoice_balances: dict[str, dict] = Field(default_factory=dict)
    invoice_case_ids: dict[str, str]
    invoices: list[InvoiceOut]
    cases: list[RecoveryCaseListItemOut]
    activities: list[ActivityRecord]
    page: int
    page_size: int
    invoice_total: int
    case_total: int
    activity_total: int
    semantics: dict[str, str]
    historical_as_of_available: bool = False
    historical: HistoricalSnapshot | None = None
    inference_telemetry: dict[str, InferenceTelemetry] = Field(default_factory=dict)
    inference_records: list[InferenceRecordOut] = Field(default_factory=list)
    inference_total: int = 0
    inference_requests: int | None = None
    inference_latency_ms: float | None = None
    inference_errors: int | None = None


def resolve_period(period: str, end: date) -> tuple[date, date]:
    """Inclusive rolling calendar-month windows; month ends clamp safely."""
    from calendar import monthrange
    if period in {'7D', '30D'}:
        return end - timedelta(days=6 if period == '7D' else 29), end
    if period == 'YTD':
        return date(end.year, 1, 1), end
    months = {'1M': 1, '3M': 3, '6M': 6, '12M': 12}[period]
    exclusive = end + timedelta(days=1)
    ordinal = exclusive.year * 12 + exclusive.month - 1 - months
    year, month = divmod(ordinal, 12)
    month += 1
    return date(year, month, min(exclusive.day, monthrange(year, month)[1])), end
