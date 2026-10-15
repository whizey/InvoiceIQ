"""Phase 5: real PostgreSQL and HTTP cross-company reviewer isolation."""

import json
import re
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.api import recovery_cases as recovery_api
from app.core.db import async_session_factory, engine
from scripts.test_database_safety import HOST, HOST_PORT
from app.core.reviewer_auth import ReviewerIdentity, require_reviewer
from app.main import app
from app.models import AuditLog, RecoveryAction
from app.models.enums import RecoveryActionStatus
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_review_gate import (
    _actions,
    _high_value_case,
    run_recovery_cycle,
    seeded_db,  # noqa: F401 - pytest registers the imported fixture
)

EXPECTED_DB = "invoiceiq_phase1_regression_test"


async def snapshot(action_id, case_id):
    async with async_session_factory() as session:
        action = await session.get(RecoveryAction, action_id)
        assert action is not None

        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.recovery_case_id == case_id)
        )

        tables = (
            await session.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' "
                    "AND tablename ILIKE '%outbox%' "
                    "ORDER BY tablename"
                )
            )
        ).scalars().all()

        assert tables, "Expected transactional outbox table was not found"

        outbox_counts = {}

        for table_name in tables:
            assert re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", table_name)
            count = await session.scalar(
                text(f'SELECT count(*) FROM public."{table_name}"')
            )
            outbox_counts[table_name] = count

        return {
            "status": str(action.status),
            "executed_at": str(action.executed_at),
            "result": json.dumps(action.result, sort_keys=True, default=str),
            "audit_count": audit_count,
            "outbox_counts": outbox_counts,
        }




@pytest_asyncio.fixture
async def isolated_engine_pool():
    """Prevent reuse of asyncpg connections across pytest event loops."""
    await engine.dispose()
    try:
        yield
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_other_company_cannot_mutate_real_action(
    monkeypatch, decision, seeded_db, isolated_engine_pool  # noqa: F811 - pytest fixture injection
):
    assert (
        engine.url.host,
        engine.url.port,
        engine.url.database,
    ) == (
        HOST,
        HOST_PORT,
        EXPECTED_DB,
    ), "STOP: Refusing to run against a non-isolated database"

    tag = uuid4().hex[:10]

    allowed_case = await _high_value_case(f"P5-ALLOWED-{tag}")
    denied_case = await _high_value_case(f"P5-DENIED-{tag}")

    assert allowed_case.company_id != denied_case.company_id

    async with async_session_factory() as session:
        await run_recovery_cycle(session, denied_case.id)

    actions = await _actions(denied_case.id)
    pending = [
        item for item in actions
        if item.status == RecoveryActionStatus.PENDING_APPROVAL
    ]
    assert pending, "No pending approval action was created"

    action_id = pending[0].id
    before = await snapshot(action_id, denied_case.id)

    monkeypatch.setenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES",
        json.dumps({
            "reviewer-company-a": [str(allowed_case.company_id)]
        }),
    )
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")

    async def reviewer_override():
        return ReviewerIdentity(reviewer_id="reviewer-company-a")

    forbidden_service = AsyncMock(
        side_effect=AssertionError(
            "Cross-company reviewer reached the execution service"
        )
    )
    monkeypatch.setattr(
        recovery_api.review_gate,
        decision,
        forbidden_service,
    )

    previous_overrides = app.dependency_overrides.copy()

    try:
        app.dependency_overrides[require_reviewer] = reviewer_override

        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/recovery-cases/actions/{action_id}/{decision}",
                json={"note": "Phase 5 cross-company check"},
            )

    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)

    after = await snapshot(action_id, denied_case.id)

    assert response.status_code == 404, response.text
    assert before == after, (
        "Unauthorized HTTP request changed financial review state"
    )
    forbidden_service.assert_not_awaited()
