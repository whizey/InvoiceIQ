"""Real Kafka publisher, used when KAFKA_BOOTSTRAP_SERVERS is configured."""

import asyncio
import json
import logging

from aiokafka import AIOKafkaProducer

logger = logging.getLogger("app.events")

# Bounds how long a publish can ever add to the request that triggered it —
# if Kafka is configured but unreachable, callers must not hang waiting on it.
PUBLISH_TIMEOUT_SECONDS = 5


class KafkaEventPublisher:
    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._producer: AIOKafkaProducer | None = None

    async def _get_producer(self) -> AIOKafkaProducer:
        if self._producer is None:
            producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
            await producer.start()
            self._producer = producer
        return self._producer

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        try:
            await asyncio.wait_for(self._publish(topic, key, payload), timeout=PUBLISH_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("failed to publish Kafka event topic=%s key=%s", topic, key, exc_info=True)

    async def _publish(self, topic: str, key: str, payload: dict) -> None:
        producer = await self._get_producer()
        await producer.send_and_wait(
            topic,
            key=key.encode("utf-8"),
            value=json.dumps(payload, default=str).encode("utf-8"),
        )

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
