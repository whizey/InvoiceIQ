import csv
import io
from typing import Annotated
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.analytics_auth import AnalyticsIdentity, require_analytics
from app.core.db import get_db
from app.schemas.preserved_dashboard import DashboardSelection, PreservedDashboard
from app.services.preserved_dashboard import build_dashboard

router = APIRouter(prefix="/dashboard/preserved", tags=["existing dashboard adapter"])


@router.get("", response_model=PreservedDashboard)
async def overview(
    selection: Annotated[DashboardSelection, Query()],
    identity: AnalyticsIdentity = Depends(require_analytics),
    db: AsyncSession = Depends(get_db),
):
    return await build_dashboard(db, selection, identity)


@router.get("/export")
async def export(
    selection: Annotated[DashboardSelection, Query()],
    identity: AnalyticsIdentity = Depends(require_analytics),
    db: AsyncSession = Depends(get_db),
):
    result = await build_dashboard(db, selection, identity, export=True)
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    fields = [
        "invoice_number",
        "company",
        "posting_date",
        "due_date",
        "amount_total",
        "amount_paid",
        "balance",
        "days_past_due",
        "status",
        "probability",
        "case_id",
    ]
    writer.writerow([*fields, "currency", "currency_verified", "as_of"])
    for row in result.invoices:
        data = row.model_dump()
        values = []
        for field in fields:
            value = data[field]
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            values.append(value)
        writer.writerow([*values, result.currency, result.currency_verified, result.as_of])
    return Response(
        "\ufeff" + out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="invoiceiq.csv"', "Cache-Control": "no-store"},
    )
