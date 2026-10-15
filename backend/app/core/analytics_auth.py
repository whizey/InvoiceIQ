"""Explicit workspace scopes for analytics; companies are customer partitions.

ANALYTICS_API_KEYS maps workspace IDs to {token, company_ids}. An empty scope
has no records. There is deliberately no wildcard or implicit reviewer access.
"""
import hmac
import json
import os
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPAuthorizationCredentials
from app.core.reviewer_auth import bearer_scheme
from app.core.db import get_db
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AnalyticsIdentity:
    workspace_id: str
    company_ids: tuple[UUID, ...]


def configured_scopes() -> list[tuple[str, str, tuple[UUID, ...]]]:
    try:
        config = json.loads(os.environ.get("ANALYTICS_API_KEYS", "{}"))
        if not isinstance(config, dict) or not config:
            raise ValueError()
        scopes = [(name, entry["token"], tuple(UUID(value) for value in entry["company_ids"]))
                  for name, entry in config.items()]
        if any(not isinstance(name, str) or not name or not isinstance(token, str) or not token
               for name, token, _ in scopes):
            raise ValueError()
        if len({token for _, token, _ in scopes}) != len(scopes):
            raise ValueError()
        return scopes
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, "Scoped analytics authentication is not configured") from None


async def require_analytics(credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
                            db: AsyncSession = Depends(get_db)) -> AnalyticsIdentity:
    if os.getenv("DEMO_SYNTHETIC", "").lower() == "true":
        if os.getenv("PUBLIC_READ_ONLY", "").lower() != "true":
            raise HTTPException(503, "Synthetic demo requires read-only mode")
        from app.seed.synthetic_demo import synthetic_demo_scope
        try:
            companies = await synthetic_demo_scope(db)
        except RuntimeError:
            raise HTTPException(503, "Synthetic demo provenance is unavailable") from None
        return AnalyticsIdentity("Synthetic demo (simulated actions)", companies)
    if os.getenv("PUBLIC_READ_ONLY", "").lower() == "true":
        from app.api.public_analytics import public_dataset_db
        from app.models import Company, DatasetImport
        from sqlalchemy import select
        import re
        await public_dataset_db(db)
        if await db.get(DatasetImport, "public_b2b_dataset") is None:
            raise HTTPException(503, "Real public dataset has not been reconciled")
        customers = (await db.execute(select(Company.id, Company.name))).all()
        if any(not re.fullmatch(r"Customer \d{4}", name) for _, name in customers):
            raise HTTPException(503, "Public dataset requires pseudonymized customer names")
        return AnalyticsIdentity("public_b2b_dataset", tuple(cid for cid, _ in customers))
    scopes = configured_scopes()
    if credentials is None:
        raise HTTPException(401, "Analytics authentication required", headers={"WWW-Authenticate": "Bearer"})
    for workspace, token, companies in scopes:
        if hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            return AnalyticsIdentity(workspace, companies)
    if credentials.credentials.startswith("iqs_"):
        from app.core.workspace_sessions import lookup_session
        session = await lookup_session(db, credentials.credentials, scopes)
        if session is not None:
            return AnalyticsIdentity(*session)
    raise HTTPException(403, "Invalid analytics credentials")
