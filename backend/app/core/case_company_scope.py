"""Case-level company authorization for recovery and communications."""

import os
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.models import RecoveryCase


async def require_case_company_scope(
    case_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Authorize a case before any recovery or communication side effect."""

    if os.getenv("PUBLIC_READ_ONLY", "").lower() == "true":
        raise HTTPException(
            status_code=403,
            detail="Mutations are disabled in public read-only mode",
        )

    hosted = settings.env.lower() in {
        "production", "staging", "prod"
    }
    configured = bool(
        os.getenv("ANALYTICS_API_KEYS", "").strip()
    )

    if not hosted and not configured:
        return

    allowed = getattr(request.state, "analytics_scope", None)

    if allowed is None:
        raise HTTPException(
            status_code=401,
            detail="Scoped analytics authentication required",
        )

    case = await db.get(RecoveryCase, case_id)

    if case is None or str(case.company_id) not in {
        str(company_id) for company_id in allowed
    }:
        raise HTTPException(
            status_code=404,
            detail="Recovery case not found",
        )
