from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass

from fastapi import HTTPException, Security, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)


bearer_scheme = HTTPBearer(
    auto_error=False,
)


@dataclass(frozen=True, slots=True)
class ReviewerIdentity:
    reviewer_id: str


def _configured_reviewers() -> dict[str, str]:
    raw = os.getenv(
        "REVIEWER_API_KEYS",
        "",
    ).strip()

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reviewer authentication is not configured",
        )

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reviewer authentication is misconfigured",
        ) from exc

    if not isinstance(parsed, dict) or not parsed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reviewer authentication is misconfigured",
        )

    reviewers: dict[str, str] = {}

    for reviewer_id, api_key in parsed.items():
        if (
            not isinstance(reviewer_id, str)
            or not reviewer_id.strip()
            or not isinstance(api_key, str)
            or not api_key.strip()
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Reviewer authentication is misconfigured",
            )

        reviewers[reviewer_id.strip()] = api_key.strip()

    if len(set(reviewers.values())) != len(reviewers):
        raise HTTPException(status_code=503, detail="Reviewer authentication is misconfigured")
    return reviewers


async def require_reviewer(
    credentials: HTTPAuthorizationCredentials | None = Security(
        bearer_scheme
    ),
) -> ReviewerIdentity:
    reviewers = _configured_reviewers()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Reviewer authentication required",
            headers={
                "WWW-Authenticate": "Bearer",
            },
        )

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Reviewer authentication required",
            headers={
                "WWW-Authenticate": "Bearer",
            },
        )

    supplied_key = credentials.credentials

    for reviewer_id, expected_key in reviewers.items():
        if hmac.compare_digest(
            supplied_key.encode(),
            expected_key.encode(),
        ):
            return ReviewerIdentity(
                reviewer_id=reviewer_id
            )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid reviewer credentials",
    )
