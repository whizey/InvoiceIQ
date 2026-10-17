"""Delivery when no Kafka broker is configured.

The same idempotent handlers run in-process right after the business transaction commits, so the
product behaves the same with or without a broker. With KAFKA_BOOTSTRAP_SERVERS set this module is
not used: the outbox dispatcher publishes to Kafka and the consumer applies the events.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import update

from app.core.config import settings
from app.events.handlers import handle_message
from app.models.outbox import OutboxEvent

logger = logging.getLogger("app.events.inline")


def uses_broker() -> bool:
    return bool(settings.kafka_bootstrap_servers)


async def deliver_inline(session_factory, topic: str, envelope: dict) -> str:
    outcome = await handle_message(session_factory, topic, envelope, max_attempts=2, backoff=lambda attempt: 0.2)
    if outcome != "dead_lettered":
        async with session_factory() as session:
            async with session.begin():
                await session.execute(update(OutboxEvent).where(OutboxEvent.id == uuid.UUID(envelope["event_id"]))
                                      .values(published_at=datetime.now(timezone.utc), delivery_mode="inline"))
    return outcome
