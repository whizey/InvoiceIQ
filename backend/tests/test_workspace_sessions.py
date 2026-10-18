"""Revocable viewer sessions cannot cross tenants or act as reviewers."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, insert, update

from app.api.auth import _check_rate_limit
from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.core.workspace_sessions import digest, workspace_sessions
from app.main import app
from app.models import Company
from app.models.enums import CompanySegment


@pytest.mark.asyncio
async def test_workspace_session_is_scoped_expiring_and_revocable(monkeypatch):
    one, two = uuid4(), uuid4()
    key_a, key_b = "test-workspace-a-key-with-enough-entropy", "test-workspace-b-key-with-enough-entropy"
    monkeypatch.setenv("ANALYTICS_API_KEYS", json.dumps({
        "alpha": {"token": key_a, "company_ids": [str(one)]},
        "beta": {"token": key_b, "company_ids": [str(two)]},
    }))
    monkeypatch.setenv("REVIEWER_API_KEYS", json.dumps({"reviewer": "test-reviewer-key-with-enough-entropy"}))
    monkeypatch.setattr(settings, "env", "staging")

    async with async_session_factory() as db:
        await db.execute(insert(Company), [
            {"id": one, "name": "Auth Synthetic A", "segment": CompanySegment.SMB},
            {"id": two, "name": "Auth Synthetic B", "segment": CompanySegment.SMB},
        ])
        await db.commit()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/companies")).status_code == 401
            assert (await client.post("/auth/sessions", headers={"Authorization": "Bearer wrong"})).status_code == 401
            issued = await client.post("/auth/sessions", headers={"Authorization": f"Bearer {key_a}"})
            assert issued.status_code == 200
            session = issued.json()["token"]
            assert session.startswith("iqs_") and "no-store" in issued.headers["cache-control"]
            headers = {"Authorization": f"Bearer {session}"}
            companies = await client.get("/companies", headers=headers)
            assert companies.status_code == 200
            assert {row["id"] for row in companies.json()} == {str(one)}
            assert (await client.get(f"/companies/{two}", headers=headers)).status_code == 404
            assert (await client.post(f"/recovery-cases/actions/{uuid4()}/approve", headers=headers)).status_code == 403

            revoked = await client.delete("/auth/sessions/current", headers=headers)
            assert revoked.status_code == 200
            assert (await client.get("/companies", headers=headers)).status_code == 403

            renewed = await client.post("/auth/sessions", headers={"Authorization": f"Bearer {key_a}"})
            token = renewed.json()["token"]
            async with async_session_factory() as db:
                await db.execute(update(workspace_sessions).where(
                    workspace_sessions.c.token_digest == digest(token)
                ).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
                await db.commit()
            assert (await client.get("/companies", headers={"Authorization": f"Bearer {token}"})).status_code == 403

            rotated = await client.post("/auth/sessions", headers={"Authorization": f"Bearer {key_a}"})
            rotated_token = rotated.json()["token"]
            monkeypatch.setenv("ANALYTICS_API_KEYS", json.dumps({
                "alpha": {"token": "rotated-workspace-key-with-enough-entropy", "company_ids": [str(one)]}
            }))
            assert (await client.get("/companies", headers={"Authorization": f"Bearer {rotated_token}"})).status_code == 403
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Company).where(Company.id.in_([one, two])))
            await db.commit()
        await engine.dispose()


def test_auth_attempts_are_bounded():
    address = f"synthetic-{uuid4()}"
    for _ in range(30):
        _check_rate_limit(address)
    with pytest.raises(HTTPException) as error:
        _check_rate_limit(address)
    assert error.value.status_code == 429
