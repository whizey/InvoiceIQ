"""Opt-in public deployment guard; mutation protection precedes all routes."""
import os
import time
from collections import OrderedDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class PublicReadOnlyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.requests = OrderedDict()

    async def dispatch(self, request, call_next):
        if os.getenv('PUBLIC_READ_ONLY', '').lower() != 'true':
            return await call_next(request)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            return JSONResponse({'detail': 'Public dataset deployment is read-only'}, status_code=403)
        origin = request.headers.get('origin')
        allowed = os.getenv('PUBLIC_FRONTEND_ORIGIN', '')
        if origin and origin != allowed:
            return JSONResponse({'detail': 'Origin is not allowed'}, status_code=403)
        now = time.monotonic()
        key = request.client.host if request.client else 'unknown'
        began, count = self.requests.pop(key, (now, 0))
        if now - began >= 60:
            began, count = now, 0
        self.requests[key] = (began, count + 1)
        while len(self.requests) > 10000:
            self.requests.popitem(last=False)
        if count >= 120:
            return JSONResponse({'detail': 'Rate limit exceeded'}, status_code=429, headers={'Retry-After': '60'})
        response = await call_next(request)
        if origin and origin == allowed:
            response.headers['Access-Control-Allow-Origin'] = allowed
            response.headers['Vary'] = 'Origin'
        return response
