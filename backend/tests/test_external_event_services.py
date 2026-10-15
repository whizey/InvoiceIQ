"""Opt-in real services, isolated keys/group IDs; never dispatch customer tasks."""
import asyncio
import json
import os
from uuid import uuid4
import pytest
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def isolated_database_connections():
    from app.core.db import engine
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_kafka_acknowledgement_and_consumer():
    address = os.getenv("TEST_KAFKA_BOOTSTRAP_SERVERS")
    if not address:
        pytest.skip("Real Kafka not configured: set TEST_KAFKA_BOOTSTRAP_SERVERS")
    from aiokafka import AIOKafkaConsumer
    from app.events.kafka_publisher import KafkaEventPublisher
    topic = "invoiceiq.acceptance." + uuid4().hex
    event_id = str(uuid4())
    publisher = KafkaEventPublisher(address)
    consumer = AIOKafkaConsumer(topic, bootstrap_servers=address, group_id="acceptance-"+uuid4().hex,
        auto_offset_reset="earliest", enable_auto_commit=False)
    try:
        await consumer.start()
        await publisher.publish_reliably(topic, "synthetic-test-only", {"event_id":event_id})
        message = await asyncio.wait_for(consumer.getone(), timeout=30)
        assert json.loads(message.value)["event_id"] == event_id
    finally:
        await consumer.stop()
        await publisher.close()


@pytest.mark.asyncio
async def test_real_redis_lock_exclusion_and_release(monkeypatch):
    address = os.getenv("TEST_REDIS_URL")
    if not address:
        pytest.skip("Real Redis not configured: set TEST_REDIS_URL (dedicated test DB)")
    from app.core.config import settings
    from app.core.locks import acquire_lock, LockAcquisitionError
    import redis.asyncio as redis
    monkeypatch.setattr(settings,"redis_url",address)
    monkeypatch.setattr(settings,"env","test")
    client = redis.from_url(address)
    key="acceptance-only-"+uuid4().hex
    try:
        assert await client.ping()
        async with acquire_lock(key):
            assert await client.get("lock:"+key) is not None, "Must hold an actual Redis lock, not local fallback"
            assert await client.ttl("lock:"+key) > 0
            with pytest.raises(LockAcquisitionError):
                async with acquire_lock(key):
                    pytest.fail("Concurrent action lock was granted")
        assert await client.get("lock:"+key) is None
        async with acquire_lock(key):
            assert await client.get("lock:"+key) is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_real_kafka_transactional_outbox_failure_retry_and_idempotency():
    address = os.getenv("TEST_KAFKA_BOOTSTRAP_SERVERS")
    if not address:
        pytest.skip("Real Kafka not configured")
    from datetime import datetime, timezone
    from sqlalchemy import delete, select
    from app.core.db import async_session_factory, engine
    from app.events import topics
    from app.events.kafka_publisher import KafkaEventPublisher
    from app.events.outbox import TransactionalEventPublisher, dispatch_batch
    from app.models import OutboxEvent
    from aiokafka import AIOKafkaConsumer
    await engine.dispose()
    key = "synthetic-acceptance-" + uuid4().hex
    async with async_session_factory() as db:
        await TransactionalEventPublisher(db).publish(topics.RISK_SCORED, key, {"synthetic":True})
        await db.flush()
        event = (await db.execute(select(OutboxEvent).where(OutboxEvent.key == key))).scalar_one()
        event_id = event.id
        await db.commit()
    # An actual refused TCP connection exercises durable backoff without a broker mock.
    unavailable = KafkaEventPublisher("127.0.0.1:1")
    real = KafkaEventPublisher(address)
    consumer = AIOKafkaConsumer(topics.RISK_SCORED, bootstrap_servers=address,
        group_id="acceptance-"+uuid4().hex, auto_offset_reset="earliest", enable_auto_commit=False)
    try:
        assert await dispatch_batch(async_session_factory, unavailable, event_ids={event_id}) == {"delivered":0,"failed":1}
        async with async_session_factory() as db:
            row = await db.get(OutboxEvent,event_id)
            assert row.attempts == 1 and row.published_at is None
            assert row.available_at > row.created_at
            row.available_at = datetime.now(timezone.utc)
            await db.commit()
        await consumer.start()
        assert await dispatch_batch(async_session_factory, real, event_ids={event_id}) == {"delivered":1,"failed":0}
        async def find_event():
            async for message in consumer:
                if message.key.decode() == key:
                    return json.loads(message.value)
        received = await asyncio.wait_for(find_event(),timeout=30)
        assert received["event_id"] == str(event_id)
        async with async_session_factory() as db:
            row = await db.get(OutboxEvent,event_id)
            assert row.attempts == 2 and row.published_at is not None and row.delivery_mode == "kafka"
        assert await dispatch_batch(async_session_factory, real, event_ids={event_id}) == {"delivered":0,"failed":0}
    finally:
        await consumer.stop()
        await unavailable.close()
        await real.close()
        async with async_session_factory() as db:
            await db.execute(delete(OutboxEvent).where(OutboxEvent.id == event_id))
            await db.commit()
