import asyncio
from decimal import Decimal

import pytest

from app.core.db import async_session_factory, engine
from app.seed.run import seed
from app.services.metrics import get_dashboard_metrics

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    asyncio.run(seed())
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def test_dashboard_metrics_match_seed_scenarios():
    # Active (OPEN/MONITORING/ESCALATED) cases: Northwind 450000, Bluepeak 210000,
    # Sundial 320000, Vertex 1800000. Aarav is CLOSED (recovered), so excluded.
    async with async_session_factory() as session:
        metrics = await get_dashboard_metrics(session)

    assert metrics.total_revenue_at_risk == Decimal("2780000.00")
    assert metrics.total_revenue_recovered == Decimal("95000.00")
    assert metrics.active_cases == 4
    assert metrics.escalated_cases == 1

    expected_rate = 95000 / (95000 + 2780000)
    assert metrics.recovery_rate == pytest.approx(expected_rate)

    # Active cases are 5, 30, 60, 15 days overdue.
    assert metrics.average_days_overdue == pytest.approx((5 + 30 + 60 + 15) / 4)

    assert sum(metrics.cases_by_risk_level.values()) == 5
