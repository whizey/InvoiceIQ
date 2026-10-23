"""Server-Timing on every response: total time to first byte, and time spent in database queries.

Each database round trip is costly on the hosted setup (the API and the database are separate free services),
so this is the number to watch when a page feels slow. Browsers show it in the network panel; curl -i shows
the header. Nothing about the request or its data is included, only durations and a query count.
"""
from __future__ import annotations

import contextvars
import time

from sqlalchemy import event

_timing: contextvars.ContextVar[dict | None] = contextvars.ContextVar("server_timing", default=None)


def install_query_timing(sync_engine) -> None:
    @event.listens_for(sync_engine, "before_cursor_execute")
    def _start(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("_query_started", []).append(time.perf_counter())

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _stop(conn, cursor, statement, parameters, context, executemany):
        started = conn.info.get("_query_started")
        timing = _timing.get()
        if started and timing is not None:
            timing["db"] += time.perf_counter() - started.pop()
            timing["queries"] += 1
        elif started:
            started.pop()


class ServerTimingMiddleware:
    """Pure ASGI, so it never buffers a streamed response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        timing = {"db": 0.0, "queries": 0, "start": time.perf_counter()}
        token = _timing.set(timing)

        async def send_with_timing(message):
            if message["type"] == "http.response.start":
                total = (time.perf_counter() - timing["start"]) * 1000
                value = (f'app;dur={total:.1f}, db;dur={timing["db"] * 1000:.1f};'
                         f'desc="{timing["queries"]} queries"')
                message = {**message, "headers": [*message.get("headers", []),
                                                  (b"server-timing", value.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        finally:
            _timing.reset(token)
