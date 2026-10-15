"""Deny public legacy writes whenever customer portal is enabled."""
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class PortalLegacyGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if os.getenv("INVOICEIQ_PORTAL_ENABLED", "").lower() == "true":
            path = request.url.path
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not path.startswith(("/portal/", "/auth/")):
                return JSONResponse({"detail": "Legacy writes disabled on public SaaS"}, status_code=403)
        return await call_next(request)
