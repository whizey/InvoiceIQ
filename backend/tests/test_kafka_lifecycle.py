import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.events import kafka_publisher


@pytest.mark.asyncio
async def test_concurrent_publish_starts_one_idempotent_producer(monkeypatch):
    producer = AsyncMock()
    factory = MagicMock(return_value=producer)
    monkeypatch.setattr(kafka_publisher, "AIOKafkaProducer", factory)
    publisher = kafka_publisher.KafkaEventPublisher("mock:9092")
    await asyncio.gather(*(publisher.publish_reliably("risk.scored", "case", {}) for _ in range(3)))
    factory.assert_called_once_with(bootstrap_servers="mock:9092", enable_idempotence=True)
    producer.start.assert_awaited_once()
    assert producer.send_and_wait.await_count == 3
    await publisher.close()
    producer.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_start_closes_partial_producer_and_propagates(monkeypatch):
    producer = AsyncMock()
    producer.start.side_effect = ConnectionError("mock startup failure")
    monkeypatch.setattr(kafka_publisher, "AIOKafkaProducer", MagicMock(return_value=producer))
    publisher = kafka_publisher.KafkaEventPublisher("mock:9092")
    with pytest.raises(ConnectionError):
        await publisher.publish_reliably("risk.scored", "case", {})
    producer.stop.assert_awaited_once()
    assert publisher._producer is None
