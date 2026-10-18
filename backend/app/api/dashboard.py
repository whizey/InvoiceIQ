from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas.dashboard import DashboardMetricsOut, PolicyOverrideStatsOut
from app.services.metrics import get_dashboard_metrics, get_policy_override_stats

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/metrics", response_model=DashboardMetricsOut)
async def dashboard_metrics(db: AsyncSession = Depends(get_db)) -> DashboardMetricsOut:
    metrics = await get_dashboard_metrics(db)
    return DashboardMetricsOut(**metrics.__dict__)


@router.get("/policy-overrides", response_model=PolicyOverrideStatsOut)
async def dashboard_policy_overrides(db: AsyncSession = Depends(get_db)) -> PolicyOverrideStatsOut:
    """AI-vs-policy divergence: how often the deterministic policy engine
    overrode the LLM/rule-based recommendation, and by which rule."""
    stats = await get_policy_override_stats(db)
    return PolicyOverrideStatsOut(**stats.__dict__)


from datetime import date  # noqa: E402
from typing import Literal  # noqa: E402
from uuid import UUID  # noqa: E402
from fastapi import HTTPException, Query  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from app.core.analytics_auth import AnalyticsIdentity, require_analytics  # noqa: E402
from app.schemas.analytics import AnalyticsOut, AnalyticsRange  # noqa: E402
from app.models.enums import InvoiceStatus, RecoveryCaseStatus  # noqa: E402
from app.services.date_analytics import get_date_analytics  # noqa: E402


@router.get("/analytics", response_model=AnalyticsOut)
async def dashboard_analytics(
    start_date: date | None = None, end_date: date | None = None,
    period: Literal["7D", "30D", "1M", "3M", "6M", "12M", "YTD", "Custom"] = "3M",
    grain: Literal["month", "quarter", "year"] = "month",
    workflow_date_axis: Literal["event", "invoice"] = "event",
    case_id: UUID | None = None,
    approval_status: Literal["PENDING_APPROVAL", "REVIEW_APPROVED", "REVIEW_REJECTED"] | None = None,
    policy_reason: str = Query("", max_length=100), action: str = Query("", max_length=60),
    currency: str = Query("INR", pattern=r"^[A-Z]{3}$"),
    page: int = Query(1, ge=1, le=1000000), page_size: int = Query(25, ge=1, le=100),
    search: str = Query("", max_length=100),
    invoice_status: InvoiceStatus | None = None,
    case_status: RecoveryCaseStatus | None = None,
    risk: Literal["LOW", "MEDIUM", "HIGH", "UNSCORED"] | None = None,
    sort: Literal["date_desc", "date_asc", "due_desc", "due_asc", "amount_desc", "amount_asc"] = "date_desc",
    view: Literal["overview", "invoices", "cases", "investigations", "approvals", "ml"] = "overview",
    identity: AnalyticsIdentity = Depends(require_analytics),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsOut:
    if start_date is None:
        if period == "Custom":
            raise HTTPException(422, "Custom period requires both start_date and end_date")
        from app.models import DatasetImport
        from app.schemas.analytics import resolve_period
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from app.core.config import settings
        imported = await db.get(DatasetImport, "public_b2b_dataset")
        end_date = end_date or (imported.dataset_as_of if imported else datetime.now(ZoneInfo(settings.business_timezone)).date())
        start_date, end_date = resolve_period(period, end_date)
    elif end_date is None:
        raise HTTPException(422, "Explicit start_date requires end_date")
    try:
        interval = AnalyticsRange(start_date=start_date, end_date=end_date, currency=currency,
                                  grain=grain, workflow_date_axis=workflow_date_axis, case_id=case_id, approval_status=approval_status, policy_reason=policy_reason, action=action,
                                  page=page, page_size=page_size, search=search, view=view, invoice_status=invoice_status, case_status=case_status, risk=risk, sort=sort)
    except ValidationError as exc:
        raise HTTPException(422, "; ".join(error["msg"] for error in exc.errors())) from None
    # get_db establishes the read-only MVCC snapshot before authorization reads.
    return await get_date_analytics(db, interval, identity)


@router.get("/calendar")
async def analytics_calendar(db: AsyncSession = Depends(get_db)) -> dict:
    """Public, non-financial configuration; no customer or credential data."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.core.config import settings
    import os
    if os.getenv("SAMPLE_SYNTHETIC", "").lower() == "true":
        from app.core.analytics_auth import require_analytics
        from app.models import DatasetImport
        from app.seed.synthetic_sample import MARKER
        await require_analytics(None, db)
        imported = await db.get(DatasetImport, MARKER)
        return {"business_timezone": settings.business_timezone,
                "today": imported.dataset_as_of.isoformat(),
                "dataset_as_of": imported.dataset_as_of.isoformat(),
                "default_period": "3M", "data_mode": "synthetic_sample",
                "workflow_mode": "simulated actions with human approval",
                "llm_mode": "deterministic_fallback"}
    if os.getenv("PUBLIC_READ_ONLY", "").lower() == "true":
        from app.api.public_analytics import public_dataset_db
        from app.models import DatasetImport
        await public_dataset_db(db)
        imported = await db.get(DatasetImport, "public_b2b_dataset")
        if imported is None:
            raise HTTPException(503, "Real public dataset has not been reconciled")
        return {"business_timezone": "UTC", "today": imported.dataset_as_of.isoformat(),
                "dataset_as_of": imported.dataset_as_of.isoformat(), "default_period": "3M",
                "data_mode": "historical_public_dataset", "workflow_mode": "simulated workflow on real invoices",
                "llm_mode": "verified_recorded_only"}
    from app.models import DatasetImport
    imported = await db.get(DatasetImport, "public_b2b_dataset")
    if imported is not None:
        return {"business_timezone": "UTC", "today": imported.dataset_as_of.isoformat(),
                "dataset_as_of": imported.dataset_as_of.isoformat(), "default_period": "3M",
                "data_mode": "historical_public_dataset", "workflow_mode": "simulated workflow on real invoices",
                "llm_mode": "verified_recorded_only"}
    return {"business_timezone": settings.business_timezone,
            "today": datetime.now(ZoneInfo(settings.business_timezone)).date().isoformat(),
            "default_period": "3M"}
