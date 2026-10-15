"""Fallback publisher used when KAFKA_BOOTSTRAP_SERVERS isn't configured."""

import logging

logger = logging.getLogger("app.events")


class LogEventPublisher:
    delivery_mode = "log"

    async def publish_reliably(self, topic: str, key: str, payload: dict) -> None:
        await self.publish(topic, key, payload)

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        # Payloads can contain private invoice facts; log delivery metadata only.
        logger.info("event logged (Kafka unavailable) topic=%s", topic)

    async def close(self) -> None:
        pass
