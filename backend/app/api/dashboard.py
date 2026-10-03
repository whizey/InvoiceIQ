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
