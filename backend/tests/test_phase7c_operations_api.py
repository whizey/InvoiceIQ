"""AI Operations endpoint authorization regression tests."""

from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

from app.api.recovery_cases import (
    get_case_agent_operations,
    router,
)


def request_with_scope(scope):
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
    })
    request.state.analytics_scope = scope
    return request


def fake_db(company_id):
    db = SimpleNamespace(
        info={},
        get=AsyncMock(
            return_value=SimpleNamespace(
                company_id=company_id,
            )
        ),
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(),
    )

    db.execute.return_value = SimpleNamespace(
        all=lambda: []
    )
    return db


def test_operations_route_is_get_only():
    routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith(
            "/{case_id}/agent-operations"
        )
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"GET"}


@pytest.mark.asyncio
async def test_missing_scope_rejected_before_database_access():
    db = fake_db(uuid4())

    with pytest.raises(HTTPException) as exc:
        await get_case_agent_operations(
            uuid4(),
            request_with_scope(None),
            db,
        )

    assert exc.value.status_code == 401
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_tenant_case_is_not_disclosed():
    db = fake_db(uuid4())

    with pytest.raises(HTTPException) as exc:
        await get_case_agent_operations(
            uuid4(),
            request_with_scope((uuid4(),)),
            db,
        )

    assert exc.value.status_code == 404
    db.scalar.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_unmigrated_database_returns_status():
    company_id = uuid4()
    db = fake_db(company_id)

    result = await get_case_agent_operations(
        uuid4(),
        request_with_scope((company_id,)),
        db,
    )

    assert result["status"] == "schema_not_migrated"
    assert result["spans"] == []
    assert result["metrics"]["p95_latency_ms"] is None
    assert result["models"] == []


@pytest.mark.asyncio
async def test_database_scope_cannot_expand_request_scope():
    company_id = uuid4()
    db = fake_db(company_id)

    db.info["analytics_scope"] = (uuid4(),)

    with pytest.raises(HTTPException) as exc:
        await get_case_agent_operations(
            uuid4(),
            request_with_scope((company_id,)),
            db,
        )

    assert exc.value.status_code == 403
    db.get.assert_not_awaited()
