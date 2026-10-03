"""Fallback publisher used when KAFKA_BOOTSTRAP_SERVERS isn't configured."""

import logging

logger = logging.getLogger("app.events")


class LogEventPublisher:
    async def publish(self, topic: str, key: str, payload: dict) -> None:
        logger.info("event (no Kafka configured) topic=%s key=%s payload=%s", topic, key, payload)

    async def close(self) -> None:
        pass
