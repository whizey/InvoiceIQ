"""Transactional, metadata-only recovery-agent tracing."""

from __future__ import annotations

import logging
import os

from datetime import datetime, timezone
from time import perf_counter_ns
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    Uuid,
    func,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RecoveryCase
from app.core.observability import request_id_var

logger = logging.getLogger(__name__)

agent_spans = Table(
    "agent_node_spans",
    MetaData(),
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("recovery_case_id", Uuid(as_uuid=True), nullable=False),
    Column("company_id", Uuid(as_uuid=True), nullable=False),
    Column("investigation_id", Uuid(as_uuid=True), nullable=False),
    Column("node_name", String(64), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("policy_decision", String(32)),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
)

ALLOWED_NODES = frozenset({
    "check_terminal",
    "load_customer_context",
    "calculate_recovery_risk",
    "diagnose_case",
    "recommend_intervention",
    "policy_check",
    "execute_action",
    "record_outcome",
})


def tracing_enabled() -> bool:
    return os.getenv(
        "INVOICEIQ_AGENT_TRACES", ""
    ).lower() in {"1", "true", "yes"}


async def persist_span(
    session: AsyncSession,
    state: dict,
    node: str,
    started_ns: int,
    result: dict,
) -> None:
    """Record a minimal node span in the workflow transaction."""

    if node not in ALLOWED_NODES:
        raise ValueError("Unknown recovery graph node")

    # Older graph unit tests intentionally use mocked sessions.
    if not isinstance(session, AsyncSession):
        return

    try:
        case_id = UUID(str(state["case_id"]))
        investigation_id = UUID(str(state["investigation_id"]))

        case = await session.get(RecoveryCase, case_id)

        if case is None:
            return

        duration_ms = round(
            max(0, perf_counter_ns() - started_ns) / 1_000_000,
            3,
        )

        decision = (
            result.get("policy_decision")
            if node == "policy_check"
            else None
        )

        if decision is not None and decision not in {
            "APPROVED",
            "REJECTED",
            "REQUIRES_HUMAN_REVIEW",
        }:
            raise ValueError("Invalid policy decision")

        # Savepoint prevents optional telemetry failure
        # from poisoning the recovery transaction.
        async with session.begin_nested():
            await session.execute(
                insert(agent_spans).values(
                    id=uuid4(),
                    recovery_case_id=case_id,
                    company_id=case.company_id,
                    investigation_id=investigation_id,
                    node_name=node,
                    outcome="ok",
                    duration_ms=duration_ms,
                    policy_decision=decision,
                    occurred_at=datetime.now(timezone.utc),
                )
            )
        details = {
            "trace_node": node,
            "investigation_id": str(investigation_id),
            "duration_ms": duration_ms,
            "policy_decision": decision,
            "outcome": "ok",
        }
        request_id = request_id_var.get()
        if request_id:
            details["request_id"] = request_id
        logger.info("Agent node completed", extra=details)

    except Exception:
        # Deliberately omit invoice content and SQL parameters.
        logger.warning(
            "Agent span unavailable; recovery policy unchanged",
            extra={
                "trace_node": node,
                "trace_failure": True,
            },
        )


async def list_case_spans(
    session: AsyncSession,
    case_id: UUID,
    *,
    authorized_company_ids,
    limit: int = 100,
) -> list[dict]:
    """List traces within an explicitly authorized tenant scope."""

    scope = tuple(authorized_company_ids or ())

    if not scope:
        raise PermissionError(
            "Authorized company scope is required"
        )

    if any(not isinstance(value, UUID) for value in scope):
        raise TypeError("Company IDs must be UUIDs")

    if not 1 <= limit <= 200:
        raise ValueError("Limit must be between 1 and 200")

    statement = (
        select(agent_spans)
        .where(
            agent_spans.c.recovery_case_id == case_id,
            agent_spans.c.company_id.in_(scope),
        )
        .order_by(
            agent_spans.c.occurred_at.desc(),
            agent_spans.c.id.desc(),
        )
        .limit(limit)
    )

    rows = (
        await session.execute(statement)
    ).mappings().all()

    return [
        {
            "id": str(row["id"]),
            "investigation_id": str(row["investigation_id"]),
            "node": row["node_name"],
            "outcome": row["outcome"],
            "duration_ms": row["duration_ms"],
            "policy_decision": row["policy_decision"],
            "occurred_at": row["occurred_at"].isoformat(),
        }
        for row in rows
    ]


async def summarize_node_spans(
    session: AsyncSession,
    *,
    authorized_company_ids,
) -> list[dict]:
    """Provide tenant-scoped dashboard timing metrics."""

    scope = tuple(authorized_company_ids or ())

    if not scope or any(
        not isinstance(value, UUID)
        for value in scope
    ):
        raise PermissionError(
            "Explicit authorized UUID company scope required"
        )

    statement = (
        select(
            agent_spans.c.node_name,
            func.count().label("runs"),
            func.avg(
                agent_spans.c.duration_ms
            ).label("avg_ms"),
            func.percentile_cont(0.95)
            .within_group(
                agent_spans.c.duration_ms
            )
            .label("p95_ms"),
        )
        .where(
            agent_spans.c.company_id.in_(scope)
        )
        .group_by(agent_spans.c.node_name)
        .order_by(agent_spans.c.node_name)
    )

    rows = (
        await session.execute(statement)
    ).mappings().all()

    return [
        {
            "node": row["node_name"],
            "runs": row["runs"],
            "average_duration_ms": round(
                float(row["avg_ms"]), 3
            ),
            "p95_duration_ms": round(
                float(row["p95_ms"]), 3
            ),
        }
        for row in rows
    ]
