from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.db import async_session_factory, engine
from app.events import topics
from app.events.outbox import TransactionalEventPublisher, dispatch_batch
from app.models import OutboxEvent


@pytest_asyncio.fixture(autouse=True)
async def isolated_connection_pool():
    await engine.dispose()
    try:
        yield
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_rollback_retry_and_delivery():
    key = str(uuid4())
    # Business rollback must also discard notification intent.
    async with async_session_factory() as session:
        await TransactionalEventPublisher(session).publish(topics.REVIEW_RESOLVED, key, {"decision": "APPROVED"})
        await session.flush()
        await session.rollback()
    async with async_session_factory() as session:
        assert (await session.execute(select(OutboxEvent).where(OutboxEvent.key == key))).scalar_one_or_none() is None
        await TransactionalEventPublisher(session).publish(topics.REVIEW_RESOLVED, key, {"decision": "APPROVED"})
        await session.commit()
        event = (await session.execute(select(OutboxEvent).where(OutboxEvent.key == key))).scalar_one()
        event_id = event.id
        envelope_id = event.payload["event_id"]

    publisher = AsyncMock()
    publisher.delivery_mode = "kafka"
    publisher.publish_reliably.side_effect = ConnectionError("mock broker unavailable")
    failed = await dispatch_batch(async_session_factory, publisher, event_ids={event_id})
    assert failed["failed"] >= 1
    async with async_session_factory() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event.published_at is None
        assert event.attempts == 1
        assert event.available_at > datetime.now(timezone.utc)
        event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    publisher.publish_reliably.side_effect = None
    await dispatch_batch(async_session_factory, publisher, event_ids={event_id})
    async with async_session_factory() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event.published_at is not None
        assert event.delivery_mode == "kafka"
        assert event.payload["event_id"] == envelope_id
        assert event.attempts == 2
    publisher.publish_reliably.reset_mock()
    await dispatch_batch(async_session_factory, publisher, event_ids={event_id})
    assert all(call.args[2]["event_id"] != envelope_id for call in publisher.publish_reliably.call_args_list)
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_event_never_enqueued():
    session = AsyncMock()
    with pytest.raises(ValueError):
        await TransactionalEventPublisher(session).publish("arbitrary.topic", "case", {})
    session.add.assert_not_called()
