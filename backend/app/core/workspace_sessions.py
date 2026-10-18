"""Short-lived, revocable sessions exchanged from existing workspace keys."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, MetaData, String, Table, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

SESSION_HOURS = 8
SESSION_PREFIX = "iqs_"

workspace_sessions = Table(
    "workspace_sessions",
    MetaData(),
    Column("token_digest", String(64), primary_key=True),
    Column("workspace_id", String(128), nullable=False),
    Column("key_digest", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def issue_session(db: AsyncSession, workspace: str, key: str) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=SESSION_HOURS)
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    await db.execute(insert(workspace_sessions).values(
        token_digest=digest(token), workspace_id=workspace, key_digest=digest(key),
        created_at=now, expires_at=expires,
    ))
    await db.commit()
    return token, expires


async def lookup_session(db: AsyncSession, token: str, scopes: list[tuple]) -> tuple | None:
    if not token.startswith(SESSION_PREFIX) or len(token) > 100:
        return None
    row = (await db.execute(select(workspace_sessions).where(
        workspace_sessions.c.token_digest == digest(token)
    ))).mappings().one_or_none()
    if row is None or row["revoked_at"] is not None or row["expires_at"] <= datetime.now(timezone.utc):
        return None
    for workspace, key, companies in scopes:
        if workspace == row["workspace_id"] and hmac.compare_digest(digest(key), row["key_digest"]):
            return workspace, companies
    return None


async def revoke_session(db: AsyncSession, token: str) -> bool:
    if not token.startswith(SESSION_PREFIX) or len(token) > 100:
        return False
    result = await db.execute(update(workspace_sessions).where(
        workspace_sessions.c.token_digest == digest(token),
        workspace_sessions.c.revoked_at.is_(None),
    ).values(revoked_at=datetime.now(timezone.utc)))
    await db.commit()
    return result.rowcount == 1
