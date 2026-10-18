import json

import pytest
from fastapi import HTTPException
from fastapi.security import (
    HTTPAuthorizationCredentials,
)

from app.core.reviewer_auth import (
    require_reviewer,
)


@pytest.mark.asyncio
async def test_reviewer_auth_accepts_valid_key(
    monkeypatch,
):
    monkeypatch.setenv(
        "REVIEWER_API_KEYS",
        json.dumps(
            {
                "reviewer-1": "secret-key-123",
            }
        ),
    )

    identity = await require_reviewer(
        HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="secret-key-123",
        )
    )

    assert identity.reviewer_id == "reviewer-1"


@pytest.mark.asyncio
async def test_reviewer_auth_rejects_invalid_key(
    monkeypatch,
):
    monkeypatch.setenv(
        "REVIEWER_API_KEYS",
        json.dumps(
            {
                "reviewer-1": "secret-key-123",
            }
        ),
    )

    with pytest.raises(
        HTTPException
    ) as exc_info:
        await require_reviewer(
            HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="wrong-key",
            )
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_reviewer_auth_requires_credentials(
    monkeypatch,
):
    monkeypatch.setenv(
        "REVIEWER_API_KEYS",
        json.dumps(
            {
                "reviewer-1": "secret-key-123",
            }
        ),
    )

    with pytest.raises(
        HTTPException
    ) as exc_info:
        await require_reviewer(None)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_reviewer_auth_requires_configuration(
    monkeypatch,
):
    monkeypatch.delenv(
        "REVIEWER_API_KEYS",
        raising=False,
    )

    credentials = (
        HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="anything",
        )
    )

    with pytest.raises(
        HTTPException
    ) as exc_info:
        await require_reviewer(
            credentials
        )

    assert exc_info.value.status_code == 503
