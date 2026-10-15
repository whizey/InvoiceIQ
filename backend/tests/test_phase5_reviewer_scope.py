"""Company-scoped reviewer authorization regression tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core import reviewer_company_scope as scope
from app.core.reviewer_auth import ReviewerIdentity


def test_configured_company_permissions(monkeypatch):
    company = uuid4()
    monkeypatch.setenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES",
        '{"reviewer-a":["' + str(company) + '"]}',
    )
    result = scope.configured_reviewer_scopes()
    assert result["reviewer-a"] == frozenset({company})


def test_malformed_company_permissions_fail_closed(monkeypatch):
    monkeypatch.setenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES",
        '{"reviewer-a":["not-a-uuid"]}',
    )
    with pytest.raises(HTTPException) as error:
        scope.configured_reviewer_scopes()
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_reviewer_authorized_for_company(monkeypatch):
    company = uuid4()
    monkeypatch.setattr(
        scope, "settings", SimpleNamespace(env="production")
    )
    monkeypatch.setenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES",
        '{"reviewer-a":["' + str(company) + '"]}',
    )
    db = AsyncMock()
    db.scalar.return_value = company
    reviewer = ReviewerIdentity(reviewer_id="reviewer-a")

    result = await scope.require_scoped_reviewer(
        action_id=uuid4(), db=db, reviewer=reviewer
    )
    assert result == reviewer
    db.scalar.assert_awaited_once()


@pytest.mark.asyncio
async def test_cross_company_reviewer_denied(monkeypatch):
    allowed_company = uuid4()
    other_company = uuid4()
    monkeypatch.setattr(
        scope, "settings", SimpleNamespace(env="production")
    )
    monkeypatch.setenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES",
        '{"reviewer-a":["' + str(allowed_company) + '"]}',
    )
    db = AsyncMock()
    db.scalar.return_value = other_company

    with pytest.raises(HTTPException) as error:
        await scope.require_scoped_reviewer(
            action_id=uuid4(),
            db=db,
            reviewer=ReviewerIdentity(reviewer_id="reviewer-a"),
        )
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_missing_hosted_configuration_fails_closed(monkeypatch):
    monkeypatch.setattr(
        scope, "settings", SimpleNamespace(env="production")
    )
    monkeypatch.delenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES", raising=False
    )
    db = AsyncMock()

    with pytest.raises(HTTPException) as error:
        await scope.require_scoped_reviewer(
            action_id=uuid4(),
            db=db,
            reviewer=ReviewerIdentity(reviewer_id="reviewer-a"),
        )
    assert error.value.status_code == 503
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_compatibility(monkeypatch):
    monkeypatch.setattr(
        scope, "settings", SimpleNamespace(env="development")
    )
    monkeypatch.delenv(
        "INVOICEIQ_REVIEWER_COMPANY_SCOPES", raising=False
    )
    db = AsyncMock()
    reviewer = ReviewerIdentity(reviewer_id="reviewer-a")

    result = await scope.require_scoped_reviewer(
        action_id=uuid4(), db=db, reviewer=reviewer
    )
    assert result == reviewer
    db.scalar.assert_not_awaited()
