"""Deny legacy data APIs on a public multi-tenant portal.

Portal identities never grant access to the old single-workspace financial API.
Host that legacy API separately until it has per-user authorization.
"""
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class PortalLegacyGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if os.getenv("INVOICEIQ_PORTAL_ENABLED", "").lower() != "true":
            return await call_next(request)
        path = request.url.path
        # Only portal business operations and non-financial health remain public.
        if path.startswith("/portal/") or path in {"/health", "/healthz", "/ready"}:
            return await call_next(request)
        if path.startswith(("/dashboard", "/analytics", "/invoices", "/companies",
                            "/recovery-cases", "/ml", "/voice")):
            return JSONResponse({"detail": "Legacy financial API unavailable on public portal"},
                                status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse({"detail": "Public portal does not allow legacy mutations"},
                                status_code=403)
        return await call_next(request)
