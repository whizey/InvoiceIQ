"""Company isolation for recovery and communication endpoints."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import get_db
from app.core.case_company_scope import require_case_company_scope
from app.main import app


@pytest.mark.asyncio
async def test_cross_company_case_is_denied(monkeypatch):
    allowed_company = uuid4()
    other_company = uuid4()

    monkeypatch.setenv(
        "ANALYTICS_API_KEYS", "configured"
    )
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")

    request = SimpleNamespace(
        state=SimpleNamespace(
            analytics_scope=(allowed_company,)
        )
    )
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(
        company_id=other_company
    )

    with pytest.raises(HTTPException) as error:
        await require_case_company_scope(
            case_id=uuid4(), request=request, db=db
        )

    assert error.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_scope_fails_closed(monkeypatch):
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS", "configured"
    )
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")

    request = SimpleNamespace(state=SimpleNamespace())
    db = AsyncMock()

    with pytest.raises(HTTPException) as error:
        await require_case_company_scope(
            case_id=uuid4(), request=request, db=db
        )

    assert error.value.status_code == 401
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_mode_blocks_case_mutation(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")

    request = SimpleNamespace(state=SimpleNamespace())
    db = AsyncMock()

    with pytest.raises(HTTPException) as error:
        await require_case_company_scope(
            case_id=uuid4(), request=request, db=db
        )

    assert error.value.status_code == 403
    db.get.assert_not_awaited()


@pytest.mark.parametrize(
    "endpoint_name,form_data",
    [
        ("run_recovery_case", None),
        ("send_reminder_email_endpoint", None),
        ("start_voice_call", None),
        (
            "submit_voice_turn",
            {
                "turn_number": "1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "typed_reply": "hello",
            },
        ),
    ],
)
def test_http_cross_company_mutation_denied(
    monkeypatch, endpoint_name, form_data
):
    allowed_company = uuid4()
    other_company = uuid4()
    case_id = uuid4()

    monkeypatch.setattr(settings, "env", "development")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS",
        json.dumps({
            "phase5-workspace": {
                "token": "phase5-test-analytics-token",
                "company_ids": [str(allowed_company)],
            }
        }),
    )

    db = AsyncMock()
    db.get.return_value = SimpleNamespace(
        company_id=other_company
    )

    async def fake_db():
        yield db

    route_templates = {
        "run_recovery_case": "/recovery-cases/{case_id}/run",
        "send_reminder_email_endpoint":
            "/recovery-cases/{case_id}/send-reminder-email",
        "start_voice_call": "/recovery-cases/{case_id}/voice/start",
        "submit_voice_turn": "/recovery-cases/{case_id}/voice/turn",
    }

    route_template = route_templates[endpoint_name]

    schema = app.openapi()
    assert route_template in schema["paths"], (
        f"Expected route is missing: {route_template}"
    )
    assert "post" in schema["paths"][route_template], (
        f"Expected POST operation is missing: {route_template}"
    )

    route_path = route_template.replace("{case_id}", str(case_id))

    previous = app.dependency_overrides.copy()

    try:
        app.dependency_overrides[get_db] = fake_db

        with TestClient(app) as client:
            response = client.post(
                route_path,
                headers={
                    "x-invoiceiq-analytics-key":
                        "phase5-test-analytics-token",
                },
                data=form_data,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Recovery case not found"

    db.get.assert_awaited_once()
    db.commit.assert_not_awaited()
