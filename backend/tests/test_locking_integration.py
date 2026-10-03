"""Proves the lock actually gates the API endpoints, not just the primitive.

Calls the route functions directly rather than through TestClient — avoids
mixing this module's event loop with TestClient's separate portal loop
(see tests/test_api_read_endpoints.py for that lesson learned in Phase 4).
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.recovery_cases import detect_overdue, run_recovery_case
from app.core.db import async_session_factory, engine
from app.core.locks import acquire_lock
from app.models import RecoveryCase
from app.seed.run import seed

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    asyncio.run(seed())
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())


async def _any_case_id():
    async with async_session_factory() as session:
        case = (await session.execute(select(RecoveryCase))).scalars().first()
        return case.id


async def test_run_recovery_case_returns_409_when_locked():
    case_id = await _any_case_id()

    async with acquire_lock(f"recovery-case:{case_id}"):
        async with async_session_factory() as session:
            with pytest.raises(HTTPException) as exc_info:
                await run_recovery_case(case_id, db=session)
        assert exc_info.value.status_code == 409


async def test_detect_overdue_returns_409_when_locked():
    async with acquire_lock("detect-overdue"):
        async with async_session_factory() as session:
            with pytest.raises(HTTPException) as exc_info:
                await detect_overdue(db=session)
        assert exc_info.value.status_code == 409


async def test_run_recovery_case_succeeds_once_lock_is_free():
    case_id = await _any_case_id()

    async with async_session_factory() as session:
        result = await run_recovery_case(case_id, db=session)

    assert result is not None
    assert result.id == case_id


async def test_run_recovery_case_404_for_missing_case():
    async with async_session_factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            await run_recovery_case(uuid.uuid4(), db=session)
    assert exc_info.value.status_code == 404
