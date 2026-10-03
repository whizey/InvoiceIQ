import pytest
from sqlalchemy import select

from app.core.db import async_session_factory
from app.models import Company, Invoice, RecoveryCase
from app.seed.run import seed


@pytest.mark.asyncio
async def test_seed_creates_expected_scenarios():
    await seed()

    async with async_session_factory() as session:
        companies = (await session.execute(select(Company))).scalars().all()
        invoices = (await session.execute(select(Invoice))).scalars().all()
        cases = (await session.execute(select(RecoveryCase))).scalars().all()

    assert len(companies) == 6
    assert len(invoices) >= 29
    assert len(cases) == 5

    case_statuses = {c.status.value for c in cases}
    assert case_statuses == {"OPEN", "MONITORING", "ESCALATED", "CLOSED"}
