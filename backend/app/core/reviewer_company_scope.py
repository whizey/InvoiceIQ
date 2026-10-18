"""Company authorization for recovery action review."""

import json
import os
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.reviewer_auth import ReviewerIdentity, require_reviewer
from app.models import RecoveryAction, RecoveryCase


def configured_reviewer_scopes() -> dict[str, frozenset[UUID]] | None:
    raw = os.getenv("INVOICEIQ_REVIEWER_COMPANY_SCOPES")
    if not raw or not raw.strip():
        return None

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected object")

        scopes = {}
        for reviewer, companies in data.items():
            if not isinstance(reviewer, str) or not reviewer.strip():
                raise ValueError("Invalid reviewer")
            if not isinstance(companies, list):
                raise ValueError("Invalid company list")
            if not all(isinstance(value, str) for value in companies):
                raise ValueError("Invalid company ID")
            scopes[reviewer] = frozenset(
                UUID(value) for value in companies
            )
        return scopes

    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Reviewer company authorization is misconfigured",
        ) from exc


async def require_scoped_reviewer(
    action_id: UUID,
    db: AsyncSession = Depends(get_db),
    reviewer: ReviewerIdentity = Depends(require_reviewer),
) -> ReviewerIdentity:
    scopes = configured_reviewer_scopes()
    hosted = settings.env.lower() in {"production", "staging", "prod"}

    if scopes is None:
        if hosted:
            raise HTTPException(
                status_code=503,
                detail="Reviewer company authorization is not configured",
            )
        return reviewer

    allowed = scopes.get(reviewer.reviewer_id, frozenset())
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="Reviewer has no permitted companies",
        )

    company_id = await db.scalar(
        select(RecoveryCase.company_id)
        .join(
            RecoveryAction,
            RecoveryAction.recovery_case_id == RecoveryCase.id,
        )
        .where(RecoveryAction.id == action_id)
    )

    if company_id is None or UUID(str(company_id)) not in allowed:
        raise HTTPException(
            status_code=404,
            detail="Action not found",
        )

    return reviewer
