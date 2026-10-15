"""Fail-closed write protection for hosted InvoiceIQ demos.

The project has no user/tenant identity system yet. Until it does, mutation
routes on hosted environments require a server-to-server operator token.
This is NOT a substitute for login, authorization, CSRF protection or tenant
isolation. Never forward this token from a public, unauthenticated Server Action.
"""

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

_HOSTED_ENVIRONMENTS = {"production", "staging", "prod"}
_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_write_authorized(*, environment: str, configured_key: str | None, provided_key: str | None) -> str:
    """Return a stable status, not a credential or sensitive diagnostic."""
    if environment.lower() not in _HOSTED_ENVIRONMENTS:
        return "local"
    if not configured_key or len(configured_key) < 24:
        return "misconfigured"
    if provided_key is None or not hmac.compare_digest(configured_key.encode(), provided_key.encode()):
        return "unauthorized"
    return "authorized"


class HostedMutationAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method.upper() in _READ_METHODS:
            return await call_next(request)

        # The two reviewer endpoints already have independent bearer-token
        # authentication. Requiring an operator key on those routes too would
        # break reviewers without providing a stronger, per-user permission.
        path = request.url.path
        if path.startswith("/portal/"):
            return await call_next(request)
        if (request.method.upper() == "POST" and path == "/auth/sessions") or (
            request.method.upper() == "DELETE" and path == "/auth/sessions/current"
        ):
            return await call_next(request)
        if path.startswith("/recovery-cases/actions/") and path.endswith(("/approve", "/reject")):
            return await call_next(request)

        outcome = is_write_authorized(
            environment=settings.env,
            configured_key=os.environ.get("INVOICEIQ_OPERATOR_API_KEY"),
            provided_key=request.headers.get("x-invoiceiq-operator-key"),
        )
        if outcome == "misconfigured":
            return JSONResponse({"detail": "Hosted write access is not configured"}, status_code=503)
        if outcome == "unauthorized":
            return JSONResponse({"detail": "Operator authentication required"}, status_code=401)
        return await call_next(request)
