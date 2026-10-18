"""Phase 7C: real PostgreSQL node tracing integration."""

from uuid import uuid4

import pytest

from sqlalchemy import select, func

from app.agents.graph import run_recovery_cycle

from app.services.agent_tracing import (
    agent_spans,
    list_case_spans,
    summarize_node_spans,
)
from app.core.observability import request_id_var

# The Phase 7B fixture lives beside this test module.
# Explicitly include the local test directory because
# pytest's import mode does not guarantee it on sys.path.
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent),
)

from test_phase7b_pg_lifecycle import (
    seed,
    isolated,  # noqa: F401 - pytest registers the imported fixture
)

pytestmark = pytest.mark.asyncio


async def test_traces_are_persisted_scoped_and_private(
    isolated,  # noqa: F811 - pytest fixture injection
    monkeypatch,
    caplog,
):
    monkeypatch.setenv(
        "INVOICEIQ_AGENT_TRACES", "1"
    )

    db, numbers = isolated

    case = await seed(
        db,
        numbers,
        amount="150000.00",
        overdue=5,
    )

    token = request_id_var.set("agent-trace-test-request")
    try:
        result = await run_recovery_cycle(
            db,
            case.id,
        )
    finally:
        request_id_var.reset(token)

    assert result["policy_decision"] == "APPROVED"

    spans = await list_case_spans(
        db,
        case.id,
        authorized_company_ids=(case.company_id,),
    )

    expected = {
        "check_terminal",
        "load_customer_context",
        "calculate_recovery_risk",
        "diagnose_case",
        "recommend_intervention",
        "policy_check",
        "execute_action",
        "record_outcome",
    }

    names = {
        entry["node"]
        for entry in spans
    }

    assert names == expected
    assert len(spans) == 8
    correlated = [record for record in caplog.records
                  if record.name == "app.services.agent_tracing" and record.message == "Agent node completed"]
    assert len(correlated) == 8
    assert {record.request_id for record in correlated} == {"agent-trace-test-request"}
    assert {record.investigation_id for record in correlated} == {spans[0]["investigation_id"]}

    # All nodes belong to the same investigation.
    assert len({
        span["investigation_id"]
        for span in spans
    }) == 1

    assert all(
        span["duration_ms"] >= 0
        for span in spans
    )

    policy = next(
        span
        for span in spans
        if span["node"] == "policy_check"
    )

    assert policy["policy_decision"] == "APPROVED"

    metrics = await summarize_node_spans(
        db,
        authorized_company_ids=(case.company_id,),
    )

    assert {
        metric["node"]
        for metric in metrics
    } == expected

    assert all(
        metric["runs"] == 1
        and metric["p95_duration_ms"] >= 0
        for metric in metrics
    )

    # Tenant isolation.
    unauthorized = await list_case_spans(
        db,
        case.id,
        authorized_company_ids=(uuid4(),),
    )

    assert unauthorized == []

    with pytest.raises(PermissionError):
        await list_case_spans(
            db,
            case.id,
            authorized_company_ids=(),
        )

    # No raw prompts, invoices, or customer information.
    allowed_keys = {
        "id",
        "investigation_id",
        "node",
        "outcome",
        "duration_ms",
        "policy_decision",
        "occurred_at",
    }

    assert all(
        set(row) == allowed_keys
        for row in spans
    )


async def test_tracing_disabled_does_not_write_spans(
    isolated,  # noqa: F811 - pytest fixture injection
    monkeypatch,
):
    monkeypatch.setenv(
        "INVOICEIQ_AGENT_TRACES", "0"
    )

    db, numbers = isolated

    case = await seed(
        db,
        numbers,
        amount="150000.00",
        overdue=5,
    )

    await run_recovery_cycle(
        db,
        case.id,
    )

    count = await db.scalar(
        select(func.count())
        .select_from(agent_spans)
        .where(
            agent_spans.c.recovery_case_id == case.id
        )
    )

    assert count == 0
