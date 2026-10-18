"""Company-scoped authorization for invoice mutations."""

import os
from uuid import UUID

from fastapi import HTTPException, Request

from app.core.config import settings


def authorize_invoice_mutation(
    request: Request,
    company_id: UUID,
    *,
    resource: str = "invoice",
) -> None:
    """Check company authorization before invoice or payment mutation."""

    if os.getenv("PUBLIC_READ_ONLY", "").lower() == "true":
        raise HTTPException(
            status_code=403,
            detail="Financial mutations are disabled in public mode",
        )

    hosted = settings.env.lower() in {
        "production", "staging", "prod"
    }

    configured = bool(
        os.getenv("ANALYTICS_API_KEYS", "").strip()
    )

    if not hosted and not configured:
        return

    allowed = getattr(
        request.state,
        "analytics_scope",
        None,
    )

    if allowed is None:
        raise HTTPException(
            status_code=401,
            detail="Scoped authentication required",
        )

    allowed_ids = {
        str(value) for value in allowed
    }

    if str(company_id) not in allowed_ids:
        detail = (
            "Company not found"
            if resource == "company"
            else "Invoice not found"
        )
        raise HTTPException(
            status_code=404,
            detail=detail,
        )
