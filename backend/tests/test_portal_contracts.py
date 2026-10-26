"""Portal authentication and read isolation contracts, no real user database needed."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.portal import SHOWCASE_ID, digest, normalize_email, password_hash, verify_password, identity
from app.models.portal import PortalWorkspace, PortalSession


def test_password_hash_and_validation():
    encoded = password_hash("a-unique-long-password-123")
    assert encoded.startswith("scrypt$")
    assert "a-unique-long-password-123" not in encoded
    assert verify_password("a-unique-long-password-123", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("anything", "broken")
    assert normalize_email("  Example@EXAMPLE.COM ") == "example@example.com"
    with pytest.raises(HTTPException):
        normalize_email("invalid")


def test_showcase_scope_is_fixed_and_token_digest_not_plaintext():
    assert digest("iqp_sample") != "iqp_sample"
    assert SHOWCASE_ID.version == 4


def request(token: str):
    return Request({"type": "http", "method": "GET", "path": "/portal/me",
                    "headers": [(b"authorization", ("Bearer " + token).encode())]})


@pytest.mark.asyncio
async def test_wrong_workspace_session_denied():
    class DB:
        async def get(self, cls, key):
            if cls is PortalSession:
                return SimpleNamespace(workspace_id=uuid.uuid4(), user_id=None, revoked_at=None,
                                       expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
            if cls is PortalWorkspace:
                return SimpleNamespace(kind="showcase")
            return None

    with pytest.raises(HTTPException) as exc:
        await identity(request("iqp_opaque"), DB())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_expired_session_denied():
    class DB:
        async def get(self, cls, key):
            if cls is PortalSession:
                return SimpleNamespace(workspace_id=SHOWCASE_ID, user_id=None, revoked_at=None,
                                       expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            return None

    with pytest.raises(HTTPException) as exc:
        await identity(request("iqp_expired"), DB())
    assert exc.value.status_code == 401
