"""Metadata-only traces of each assistant run: one row per model call, tool call, check and fallback.

No question text, answers or tool payloads are stored, only what ran, how long it took and how it ended,
so the table can answer "how reliable is the assistant?" without holding customer data.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import perf_counter

from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, Uuid, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

assistant_traces = Table(
    "assistant_traces",
    MetaData(),
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("workspace_id", Uuid(as_uuid=True), nullable=False),
    Column("run_id", Uuid(as_uuid=True), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String(16), nullable=False),
    Column("name", String(64), nullable=False),
    Column("provider", String(32)),
    Column("outcome", String(16), nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

KINDS = frozenset({"llm", "tool", "check", "policy", "fallback", "run"})


@dataclass
class Span:
    kind: str
    name: str
    provider: str | None = None
    outcome: str = "ok"
    duration_ms: float = 0.0
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TraceRecorder:
    run_id: uuid.UUID = field(default_factory=uuid.uuid4)
    spans: list[Span] = field(default_factory=list)

    def add(self, kind: str, name: str, *, outcome: str = "ok", duration_ms: float = 0.0,
            provider: str | None = None) -> Span:
        if kind not in KINDS:
            raise ValueError(f"unknown trace kind {kind!r}")
        span = Span(kind, name[:64], provider, outcome[:16], round(duration_ms, 2))
        self.spans.append(span)
        return span

    @contextmanager
    def timed(self, kind: str, name: str, provider: str | None = None):
        """Time a block; the caller may set ``span.outcome`` inside it, and an exception marks it error."""
        span = self.add(kind, name, provider=provider)
        started = perf_counter()
        try:
            yield span
        except BaseException:
            span.outcome = "error"
            raise
        finally:
            span.duration_ms = round((perf_counter() - started) * 1000, 2)

    def summary(self) -> list[dict]:
        return [{"kind": s.kind, "name": s.name, "provider": s.provider, "outcome": s.outcome,
                 "ms": s.duration_ms} for s in self.spans]

    async def persist(self, session_factory, workspace_id: uuid.UUID) -> None:
        """Write in a separate transaction; a tracing failure never breaks the answer."""
        if not self.spans:
            return
        rows = [{"id": uuid.uuid4(), "workspace_id": workspace_id, "run_id": self.run_id, "seq": i,
                 "kind": s.kind, "name": s.name, "provider": s.provider, "outcome": s.outcome,
                 "duration_ms": s.duration_ms, "occurred_at": s.occurred_at} for i, s in enumerate(self.spans)]
        try:
            async with session_factory() as session:
                await session.execute(insert(assistant_traces), rows)
                await session.commit()
        except Exception:  # noqa: BLE001 - observability must not take the assistant down
            logger.warning("assistant trace persistence failed", exc_info=True)


async def trace_summary(session: AsyncSession, workspace_id: uuid.UUID, days: int = 7) -> dict:
    """Per kind and name: calls, error rate and latency percentiles over the window."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    t = assistant_traces
    rows = (await session.execute(
        select(t.c.kind, t.c.name, func.count().label("calls"),
               func.sum((t.c.outcome != "ok").cast(Integer)).label("not_ok"),
               func.percentile_cont(0.5).within_group(t.c.duration_ms).label("p50"),
               func.percentile_cont(0.95).within_group(t.c.duration_ms).label("p95"))
        .where(t.c.workspace_id == workspace_id, t.c.occurred_at >= since)
        .group_by(t.c.kind, t.c.name).order_by(t.c.kind, t.c.name))).all()
    runs = (await session.execute(
        select(t.c.outcome, func.count()).where(t.c.workspace_id == workspace_id, t.c.occurred_at >= since,
                                                t.c.kind == "run").group_by(t.c.outcome))).all()
    return {"window_days": days, "runs": {outcome: n for outcome, n in runs},
            "steps": [{"kind": r.kind, "name": r.name, "calls": r.calls, "not_ok": int(r.not_ok or 0),
                       "p50_ms": round(r.p50 or 0, 1), "p95_ms": round(r.p95 or 0, 1)} for r in rows]}


def _share(counts: dict, total: int) -> dict:
    return {key: {"count": n, "pct": round(n * 100 / total, 1) if total else 0.0} for key, n in counts.items()}


async def reliability(session: AsyncSession, workspace_id: uuid.UUID, days: int = 7) -> dict:
    """What the reliability page shows for the live assistant: who answered, how the figures fared, how fast."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    t = assistant_traces
    scope = (t.c.workspace_id == workspace_id, t.c.occurred_at >= since)
    answered_by = dict((await session.execute(
        select(t.c.outcome, func.count()).where(*scope, t.c.kind == "run").group_by(t.c.outcome))).all())
    total = sum(answered_by.values())
    models = (await session.execute(
        select(t.c.provider, t.c.name, func.count()).where(*scope, t.c.kind == "run")
        .group_by(t.c.provider, t.c.name).order_by(func.count().desc()))).all()
    figures = dict((await session.execute(
        select(t.c.outcome, func.count()).where(*scope, t.c.kind == "check", t.c.name == "result")
        .group_by(t.c.outcome))).all())
    checked = sum(figures.values())
    advice = dict((await session.execute(
        select(t.c.outcome, func.count()).where(*scope, t.c.kind == "policy", t.c.name == "written")
        .group_by(t.c.outcome))).all())
    timing = (await session.execute(
        select(func.percentile_cont(0.5).within_group(t.c.duration_ms),
               func.percentile_cont(0.95).within_group(t.c.duration_ms))
        .where(*scope, t.c.kind == "run"))).one()
    return {
        "window_days": days, "answers": total,
        "answered_by": _share({k: answered_by.get(k, 0) for k in ("primary", "backup", "rules", "unconfigured")},
                              total),
        "models": [{"provider": p, "model": m, "answers": n} for p, m, n in models],
        "figures": _share({k: figures.get(k, 0) for k in ("verified", "repaired", "trimmed")}, checked),
        "written_advice": _share({k: advice.get(k, 0) for k in ("allowed", "blocked")}, sum(advice.values())),
        "seconds": {"p50": round((timing[0] or 0) / 1000, 2), "p95": round((timing[1] or 0) / 1000, 2)},
    }
