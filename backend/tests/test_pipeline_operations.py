"""Pipeline metrics and dead-letter replay (needs the migrated test database)."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.events import handlers, topics
from app.events.handlers import handle_message
from app.events.pipeline_stats import pipeline_stats, render_prometheus
from app.events.replay import replay_dead_letters
from app.main import app
from app.models import OutboxEvent
from app.models.event_effects import ApprovalAlert, DeadLetterEvent, ProcessedEvent


@pytest_asyncio.fixture(autouse=True)
async def isolated_connection_pool():
    await engine.dispose()
    try:
        yield
    finally:
        await engine.dispose()


def envelope(data: dict) -> dict:
    event_id = uuid4()
    return {"schema_version": 1, "event_id": str(event_id), "correlation_id": str(event_id),
            "occurred_at": datetime.now(timezone.utc).isoformat(), "data": data}


def test_prometheus_text_has_low_cardinality_labels_and_every_metric():
    text = render_prometheus({
        "outbox_unpublished": 2, "outbox_unpublished_oldest_seconds": 12.34, "awaiting_consumer": 1,
        "awaiting_consumer_oldest_seconds": 70.0, "dead_letters_by_topic": {"review.requested": 3, "other": 1},
        "events_processed": 9, "dead_letters_replayed": 4})
    for line in ("invoiceiq_outbox_unpublished_events 2", "invoiceiq_events_awaiting_consumer 1",
                 "invoiceiq_events_awaiting_consumer_oldest_seconds 70.0", "invoiceiq_events_processed_total 9",
                 'invoiceiq_dead_letters{topic="review.requested"} 3', 'invoiceiq_dead_letters{topic="other"} 1',
                 "invoiceiq_dead_letters_replayed_total 4", "# TYPE invoiceiq_events_processed_total counter"):
        assert line in text
    assert render_prometheus({**{"outbox_unpublished": 0, "outbox_unpublished_oldest_seconds": 0, "awaiting_consumer": 0,
                                 "awaiting_consumer_oldest_seconds": 0, "events_processed": 0, "dead_letters_replayed": 0},
                              "dead_letters_by_topic": {}}).count('topic="none"') == 1


@pytest.mark.asyncio
async def test_stats_count_events_stuck_between_the_broker_and_the_consumer():
    before = await pipeline_stats(async_session_factory)
    case_id = uuid4()
    stuck = envelope({"case_id": str(case_id), "reason": "published, never applied"})
    unsent = envelope({"case_id": str(case_id)})
    async with async_session_factory() as session:
        session.add_all([
            OutboxEvent(id=UUID(stuck["event_id"]), topic=topics.REVIEW_REQUESTED, key=str(case_id), payload=stuck,
                        published_at=datetime.now(timezone.utc) - timedelta(minutes=3), delivery_mode="kafka"),
            OutboxEvent(id=UUID(unsent["event_id"]), topic=topics.REVIEW_REQUESTED, key=str(case_id), payload=unsent),
        ])
        await session.commit()
    try:
        after = await pipeline_stats(async_session_factory)
        assert after["awaiting_consumer"] == before["awaiting_consumer"] + 1
        assert after["awaiting_consumer_oldest_seconds"] >= 170
        assert after["outbox_unpublished"] == before["outbox_unpublished"] + 1
        await handle_message(async_session_factory, topics.REVIEW_REQUESTED, stuck)     # the consumer catches up
        caught_up = await pipeline_stats(async_session_factory)
        assert caught_up["awaiting_consumer"] == before["awaiting_consumer"]
        assert caught_up["events_processed"] == before["events_processed"] + 1
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(ApprovalAlert).where(ApprovalAlert.case_id == case_id))
            await session.execute(delete(OutboxEvent).where(OutboxEvent.key == str(case_id)))
            await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == UUID(stuck["event_id"])))
            await session.commit()


@pytest.mark.asyncio
async def test_metrics_endpoint_includes_pipeline_health(monkeypatch):
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "env", "local")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/internal/metrics")
    assert response.status_code == 200
    assert "invoiceiq_events_awaiting_consumer" in response.text and "invoiceiq_dead_letters" in response.text
    assert "invoiceiq_http_requests_total" in response.text or "# HELP" in response.text


@pytest.mark.asyncio
async def test_replay_applies_parked_events_once_the_cause_is_fixed(monkeypatch):
    case_id = uuid4()
    event = envelope({"case_id": str(case_id), "action_type": "ESCALATION", "reason": "Parked while a bug existed"})
    working = handlers.HANDLERS[topics.REVIEW_REQUESTED]

    async def broken(session, parsed):
        raise RuntimeError("bug in the handler")

    monkeypatch.setitem(handlers.HANDLERS, topics.REVIEW_REQUESTED, broken)
    assert await handle_message(async_session_factory, topics.REVIEW_REQUESTED, event, max_attempts=1,
                                backoff=lambda attempt: 0) == "dead_lettered"
    before = await pipeline_stats(async_session_factory)
    assert before["dead_letters_by_topic"].get(topics.REVIEW_REQUESTED, 0) >= 1
    async with async_session_factory() as session:     # an unreadable message is reported, never replayed
        session.add(DeadLetterEvent(consumer=handlers.CONSUMER, event_id=uuid4(), topic=topics.RISK_SCORED,
                                    payload={"unreadable": True}, error="Malformed event envelope", attempts=1))
        await session.commit()
    try:
        # Still broken: the replay leaves the event parked.
        still = await replay_dead_letters(async_session_factory, event_id=UUID(event["event_id"]))
        assert still["replayed"] == 0 and still["still_failing"] == 1
        monkeypatch.setitem(handlers.HANDLERS, topics.REVIEW_REQUESTED, working)       # the fix ships
        dry = await replay_dead_letters(async_session_factory, event_id=UUID(event["event_id"]), dry_run=True)
        assert dry["found"] == 1 and dry["replayed"] == 0
        assert await count_alerts(case_id) == 0
        done = await replay_dead_letters(async_session_factory, topic=topics.REVIEW_REQUESTED)
        assert done["replayed"] >= 1
        assert await count_alerts(case_id) == 1
        again = await replay_dead_letters(async_session_factory, event_id=UUID(event["event_id"]))
        assert again["found"] == 0                                   # stamped as replayed, kept for audit
        assert (await replay_dead_letters(async_session_factory, topic=topics.RISK_SCORED))["unreadable"] >= 1
        after = await pipeline_stats(async_session_factory)
        assert after["dead_letters_replayed"] >= before["dead_letters_replayed"] + 1
    finally:
        async with async_session_factory() as session:
            await session.execute(delete(ApprovalAlert).where(ApprovalAlert.case_id == case_id))
            await session.execute(delete(DeadLetterEvent).where(
                (DeadLetterEvent.event_id == UUID(event["event_id"])) | (DeadLetterEvent.error == "Malformed event envelope")))
            await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == UUID(event["event_id"])))
            await session.commit()


async def count_alerts(case_id) -> int:
    async with async_session_factory() as session:
        return await session.scalar(select(func.count()).select_from(ApprovalAlert).where(ApprovalAlert.case_id == case_id))
