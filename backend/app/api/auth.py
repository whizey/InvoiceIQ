"""Exchange existing workspace credentials for revocable viewer sessions."""

import hmac
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.core.analytics_auth import configured_scopes
from app.core.db import get_db
from app.core.reviewer_auth import bearer_scheme
from app.core.workspace_sessions import issue_session, revoke_session

router = APIRouter(prefix="/auth", tags=["authentication"])
_attempts: OrderedDict[str, list[float]] = OrderedDict()


def _check_rate_limit(address: str) -> None:
    now = time.monotonic()
    attempts = [when for when in _attempts.pop(address, []) if now - when < 60]
    if len(attempts) >= 30:
        _attempts[address] = attempts
        raise HTTPException(429, "Too many authentication attempts", headers={"Retry-After": "60"})
    attempts.append(now)
    _attempts[address] = attempts
    while len(_attempts) > 10000:
        _attempts.popitem(last=False)


@router.post("/sessions")
async def create_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    _check_rate_limit(request.client.host if request.client else "unknown")
    if credentials is None or credentials.credentials.startswith("iqs_"):
        raise HTTPException(401, "Invalid workspace credentials")
    for workspace, key, _companies in configured_scopes():
        if hmac.compare_digest(credentials.credentials.encode(), key.encode()):
            token, expires = await issue_session(db, workspace, key)
            return JSONResponse(
                {"token": token, "expires_at": expires.isoformat(), "role": "viewer"},
                headers={"Cache-Control": "no-store"},
            )
    raise HTTPException(401, "Invalid workspace credentials")


@router.delete("/sessions/current")
async def delete_session(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    if credentials is None or not await revoke_session(db, credentials.credentials):
        raise HTTPException(401, "Invalid session")
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
