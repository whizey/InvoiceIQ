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
from app.events.outbox import dispatch_batch
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


async def deliver_event(session_factory, publisher, topic: str, envelope: dict) -> str:
    """Publish to the broker right after commit, or apply in-process if no broker answers.

    With a broker the consumer applies the event a moment later. If the broker is unreachable, the same
    idempotent handlers run here instead, so the product never waits on infrastructure it cannot reach.
    """
    if not uses_broker():
        return await deliver_inline(session_factory, topic, envelope)
    summary = await dispatch_batch(session_factory, publisher, limit=1, event_ids={uuid.UUID(envelope["event_id"])})
    if summary["delivered"]:
        return "kafka"
    logger.warning("Broker unavailable; applying event in-process", extra={"topic": topic})
    return await deliver_inline(session_factory, topic, envelope)
