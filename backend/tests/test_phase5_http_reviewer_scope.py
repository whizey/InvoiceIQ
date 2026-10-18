"""HTTP authorization regression tests for company-scoped reviewers."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.db import get_db
from app.core.reviewer_auth import ReviewerIdentity, require_reviewer
from app.core import reviewer_company_scope as scope
from app.api import recovery_cases as recovery_api


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_cross_company_review_cannot_reach_execution(
    monkeypatch, decision
):
    allowed_company = uuid4()
    other_company = uuid4()
    action_id = uuid4()

    monkeypatch.setattr(
        scope,
        "settings",
        SimpleNamespace(env="development"),
    )
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.delenv("ANALYTICS_API_KEYS", raising=False)
    monkeypatch.setenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES",
        json.dumps({"reviewer-a": [str(allowed_company)]}),
    )

    db = AsyncMock()
    db.scalar.return_value = other_company

    async def fake_db():
        yield db

    async def fake_reviewer():
        return ReviewerIdentity(reviewer_id="reviewer-a")

    service = AsyncMock(
        side_effect=AssertionError(
            "Unauthorized reviewer reached the review service"
        )
    )
    monkeypatch.setattr(
        recovery_api.review_gate,
        decision,
        service,
    )

    previous = app.dependency_overrides.copy()

    try:
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[require_reviewer] = fake_reviewer

        client = TestClient(app)

        try:
            response = client.post(
                f"/recovery-cases/actions/{action_id}/{decision}",
                json={"note": "HTTP company isolation test"},
            )
        finally:
            client.close()

    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Action not found"

    db.scalar.assert_awaited_once()
    db.commit.assert_not_awaited()
    service.assert_not_awaited()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_review_endpoint_fails_closed_without_hosted_scopes(
    monkeypatch, decision
):
    monkeypatch.setattr(
        scope,
        "settings",
        SimpleNamespace(env="production"),
    )
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.delenv("ANALYTICS_API_KEYS", raising=False)
    monkeypatch.delenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES",
        raising=False,
    )

    db = AsyncMock()

    async def fake_db():
        yield db

    async def fake_reviewer():
        return ReviewerIdentity(reviewer_id="reviewer-a")

    service = AsyncMock(
        side_effect=AssertionError(
            "Unconfigured reviewer reached the review service"
        )
    )
    monkeypatch.setattr(
        recovery_api.review_gate,
        decision,
        service,
    )

    previous = app.dependency_overrides.copy()

    try:
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[require_reviewer] = fake_reviewer

        client = TestClient(app)

        try:
            response = client.post(
                f"/recovery-cases/actions/{uuid4()}/{decision}",
                json={"note": "Missing scope test"},
            )
        finally:
            client.close()

    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    assert response.status_code == 503, response.text
    db.scalar.assert_not_awaited()
    db.commit.assert_not_awaited()
    service.assert_not_awaited()
