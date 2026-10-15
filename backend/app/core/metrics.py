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


MODEL_DURATION = Histogram("invoiceiq_model_inference_seconds", "Model inference latency", ("model",),
                           buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5))
MODEL_FAILURES = Counter("invoiceiq_model_inference_failures_total", "Model inference failures", ("model",))

LLM_CALLS = Counter(
    "invoiceiq_llm_calls_total", "Actual provider calls by validated outcome",
    ("stage", "provider", "outcome"),
)
LLM_DURATION = Histogram(
    "invoiceiq_llm_call_seconds", "Actual provider call latency",
    ("stage", "provider"),
    buckets=(.05, .1, .25, .5, 1, 2.5, 5, 10, 15, 30),
)
LLM_FALLBACKS = Counter(
    "invoiceiq_llm_fallbacks_total", "Deterministic LLM fallback selections",
    ("stage", "provider"),
)


def record_llm_call(stage: str, provider: str, outcome: str, duration: float) -> None:
    if not settings.metrics_enabled:
        return
    stage = stage if stage in {"diagnose", "recommend", "voice_turn"} else "other"
    provider = provider if provider in {"gemini", "openai", "groq"} else "other"
    outcome = outcome if outcome in {"success", "error", "invalid"} else "other"
    LLM_CALLS.labels(stage, provider, outcome).inc()
    LLM_DURATION.labels(stage, provider).observe(max(0, duration))


def record_llm_fallback(stage: str, provider: str) -> None:
    if not settings.metrics_enabled:
        return
    stage = stage if stage in {"diagnose", "recommend", "voice_turn"} else "other"
    provider = provider if provider in {"gemini", "openai", "groq", "fallback"} else "other"
    LLM_FALLBACKS.labels(stage, provider).inc()


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
