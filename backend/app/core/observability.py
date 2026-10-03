"""Structured logging + optional LangSmith tracing.

Every log line is one JSON object. A request_id (from the incoming
X-Request-ID header, or freshly generated) is bound to a contextvar by
RequestContextMiddleware, so any log line emitted anywhere in the call
stack while handling a request — the risk engine, a LangGraph node, the
Kafka publisher's failure path — automatically carries the same
request_id, without threading it through every function signature.

No separate collector/backend required: this writes to stdout, same as
everything else in the container. LangSmith tracing of the LangGraph
workflow is a config-only opt-in (see docs/architecture.md) — LangChain
already reports traces natively once LANGCHAIN_TRACING_V2 is set, no
code change needed here beyond bridging our Settings into os.environ.
"""

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import settings

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # We log one structured "request completed" line ourselves (see
    # RequestContextMiddleware) — uvicorn's own plain-text access log would
    # just duplicate that in a different format.
    logging.getLogger("uvicorn.access").disabled = True


def configure_langsmith() -> None:
    if not settings.langchain_tracing_v2:
        return
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
    if settings.langchain_api_key:
        os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        logger = logging.getLogger("app.request")
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed",
                extra={"method": request.method, "path": request.url.path},
            )
            raise
        else:
            # Log (and set the response header) while request_id_var is
            # still bound — resetting it in `finally` below happens last,
            # otherwise this line would silently lose its own request_id.
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            return response
        finally:
            request_id_var.reset(token)
