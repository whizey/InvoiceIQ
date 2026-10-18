"""Replays, ordering and failure handling of the event consumer (needs the migrated test database)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.db import async_session_factory, engine
from app.events import topics
from app.events.handlers import handle_message, high_risk_cases
from app.models.event_effects import ApprovalAlert, CaseRiskLatest, DeadLetterEvent, ProcessedEvent


@pytest_asyncio.fixture(autouse=True)
async def isolated_connection_pool():
    await engine.dispose()
    try:
        yield
    finally:
        await engine.dispose()


def envelope(data: dict, *, event_id=None, at=None) -> dict:
    event_id = event_id or uuid4()
    return {"schema_version": 1, "event_id": str(event_id), "correlation_id": str(event_id),
            "occurred_at": (at or datetime.now(timezone.utc)).isoformat(), "data": data}


async def count(model, *where) -> int:
    async with async_session_factory() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where))


@pytest.mark.asyncio
async def test_replaying_a_review_request_twice_creates_one_alert():
    case_id = uuid4()
    event = envelope({"case_id": str(case_id), "action_id": "a-1", "action_type": "SEND_EMAIL", "reason": "High value"})
    outcomes = [await handle_message(async_session_factory, topics.REVIEW_REQUESTED, event) for _ in range(2)]
    assert outcomes == ["processed", "duplicate"]
    assert await count(ApprovalAlert, ApprovalAlert.case_id == case_id) == 1
    assert await count(ProcessedEvent, ProcessedEvent.event_id == event["event_id"]) == 1


@pytest.mark.asyncio
async def test_high_risk_table_follows_latest_score_and_ignores_stale_or_replayed_events():
    case_id, now = uuid4(), datetime.now(timezone.utc)
    high = envelope({"case_id": str(case_id), "risk_score": 0.91, "risk_level": "HIGH"}, at=now)
    lower = envelope({"case_id": str(case_id), "risk_score": 0.2, "risk_level": "LOW"}, at=now + timedelta(minutes=5))
    stale = envelope({"case_id": str(case_id), "risk_score": 0.99, "risk_level": "CRITICAL"}, at=now - timedelta(hours=1))
    await handle_message(async_session_factory, topics.RISK_SCORED, high)
    async with async_session_factory() as session:
        assert case_id in {row.case_id for row in await high_risk_cases(session)}
    await handle_message(async_session_factory, topics.RISK_SCORED, lower)
    await handle_message(async_session_factory, topics.RISK_SCORED, stale)   # arrives late
    await handle_message(async_session_factory, topics.RISK_SCORED, high)    # replayed
    async with async_session_factory() as session:
        assert case_id not in {row.case_id for row in await high_risk_cases(session)}
        assert (await session.get(CaseRiskLatest, case_id)).risk_level == "LOW"


@pytest.mark.asyncio
async def test_an_event_that_keeps_failing_is_retried_then_dead_lettered(monkeypatch):
    from app.events import handlers
    calls = []

    async def broken(session, event):
        calls.append(event["event_id"])
        raise RuntimeError("downstream unavailable")

    monkeypatch.setitem(handlers.HANDLERS, topics.REVIEW_REQUESTED, broken)
    event = envelope({"case_id": str(uuid4())})
    dlq = AsyncMock()
    outcome = await handle_message(async_session_factory, topics.REVIEW_REQUESTED, event,
                                   dead_letter_publisher=dlq, max_attempts=3, backoff=lambda attempt: 0)
    assert outcome == "dead_lettered" and len(calls) == 3
    dlq.publish.assert_awaited_once()
    assert dlq.publish.await_args.args[0] == "review.requested.dlq"
    assert await count(DeadLetterEvent, DeadLetterEvent.event_id == event["event_id"]) == 1
    # The failed attempts rolled back their dedupe marker, so a later replay can still succeed.
    assert await count(ProcessedEvent, ProcessedEvent.event_id == event["event_id"]) == 0
    # Redelivering the same broken event does not create a second dead letter.
    await handle_message(async_session_factory, topics.REVIEW_REQUESTED, event, max_attempts=1, backoff=lambda attempt: 0)
    assert await count(DeadLetterEvent, DeadLetterEvent.event_id == event["event_id"]) == 1


@pytest.mark.asyncio
async def test_unreadable_messages_go_straight_to_the_dead_letter_table():
    dlq = AsyncMock()
    outcome = await handle_message(async_session_factory, topics.RISK_SCORED, b"not json", dead_letter_publisher=dlq,
                                   backoff=lambda attempt: 0)
    assert outcome == "dead_lettered"
    dlq.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_topics_without_a_handler_are_ignored():
    event = envelope({"case_id": str(uuid4())})
    assert await handle_message(async_session_factory, topics.INVOICE_OVERDUE, event) == "ignored"
    assert await count(ProcessedEvent, ProcessedEvent.event_id == event["event_id"]) == 0


@pytest.mark.asyncio
async def test_reconciler_applies_events_a_broker_lost_and_leaves_the_rest_alone():
    """Published to Kafka, never consumed (broker restarted): the outbox row is the durable record."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from app.events.reconcile import reconcile_outbox
    from app.models import OutboxEvent

    case_id, other_case = uuid4(), uuid4()
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lost = envelope({"case_id": str(case_id), "action_type": "ESCALATION", "reason": "Lost with the broker"})
    fresh = envelope({"case_id": str(other_case), "reason": "Published a moment ago"})
    async with async_session_factory() as session:
        session.add_all([
            OutboxEvent(id=UUID(lost["event_id"]), topic=topics.REVIEW_REQUESTED, key=str(case_id),
                        payload=lost, published_at=old, delivery_mode="kafka"),
            OutboxEvent(id=UUID(fresh["event_id"]), topic=topics.REVIEW_REQUESTED, key=str(other_case),
                        payload=fresh, published_at=datetime.now(timezone.utc), delivery_mode="kafka"),
        ])
        await session.commit()
    try:
        assert await reconcile_outbox(async_session_factory, older_than_seconds=45) >= 1
        assert await count(ApprovalAlert, ApprovalAlert.case_id == case_id) == 1
        assert await count(ApprovalAlert, ApprovalAlert.case_id == other_case) == 0   # too recent: the consumer may still get it
        # A second sweep, or the consumer finally receiving the message, changes nothing.
        await reconcile_outbox(async_session_factory, older_than_seconds=45)
        assert await handle_message(async_session_factory, topics.REVIEW_REQUESTED, lost) == "duplicate"
        assert await count(ApprovalAlert, ApprovalAlert.case_id == case_id) == 1
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(ApprovalAlert).where(ApprovalAlert.case_id.in_([case_id, other_case])))
            await session.execute(delete(OutboxEvent).where(OutboxEvent.key.in_([str(case_id), str(other_case)])))
            await session.commit()
