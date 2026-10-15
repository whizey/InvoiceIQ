"""Low-cardinality, opt-in HTTP metrics for a private Prometheus collector.

No invoice identifiers, paths with UUIDs, request bodies, tokens or user data become metric labels.
The metrics endpoint is disabled by default and authenticated separately in hosted environments.
"""

import hmac
import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

HTTP_REQUESTS = Counter(
    "invoiceiq_http_requests_total", "HTTP requests by coarse route family",
    ("method", "route_family", "status_class"),
)
HTTP_DURATION = Histogram(
    "invoiceiq_http_request_seconds", "HTTP latency by coarse route family",
    ("method", "route_family"),
    buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10),
)


def route_family(path: str) -> str:
    """Never expose case IDs, invoice IDs or attacker-controlled paths as labels."""
    segment = path.strip("/").split("/")[0]
    if segment in {"health", "companies", "invoices", "recovery-cases", "dashboard", "ml", "voice"}:
        return segment
    return "other"


def allow_metrics(*, environment: str, configured_token: str | None, provided_token: str | None) -> bool:
    if environment.lower() not in {"staging", "production"}:
        return True
    return bool(
        configured_token and len(configured_token) >= 24 and provided_token
        and hmac.compare_digest(configured_token, provided_token)
    )


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.metrics_enabled or request.url.path == "/internal/metrics":
            return await call_next(request)
        start = time.perf_counter()
        family = route_family(request.url.path)
        method = request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "OTHER"
        status = "5xx"
        try:
            response = await call_next(request)
            status = f"{response.status_code // 100}xx"
            return response
        finally:
            HTTP_REQUESTS.labels(method, family, status).inc()
            HTTP_DURATION.labels(method, family).observe(time.perf_counter() - start)


def metrics_response(request: Request) -> Response:
    if not settings.metrics_enabled:
        return Response(status_code=404)
    if not allow_metrics(
        environment=settings.env,
        configured_token=settings.metrics_token,
        provided_token=request.headers.get("x-invoiceiq-metrics-key"),
    ):
        return Response(status_code=403)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
