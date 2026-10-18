"""Safety net for events a broker may have lost.

An event the dispatcher marked as published to Kafka but that no consumer ever applied (for example because a
single-node broker with ephemeral storage restarted) would otherwise be stranded. The outbox row is the durable
record, so this sweep re-applies such rows through the same idempotent handlers. Applying an event twice is a
no-op, so running this alongside a healthy consumer is harmless.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select

from app.events.handlers import CONSUMER, HANDLERS, handle_message
from app.models.event_effects import DeadLetterEvent, ProcessedEvent
from app.models.outbox import OutboxEvent

logger = logging.getLogger("app.events.reconcile")


async def reconcile_outbox(session_factory, *, older_than_seconds: int = 45, limit: int = 200) -> int:
    """Apply published-but-unprocessed events. Returns how many were applied."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    async with session_factory() as session:
        rows = (await session.execute(
            select(OutboxEvent.id, OutboxEvent.topic, OutboxEvent.payload).where(
                OutboxEvent.topic.in_(list(HANDLERS)),
                OutboxEvent.delivery_mode == "kafka",
                OutboxEvent.published_at.is_not(None), OutboxEvent.published_at < cutoff,
                ~exists().where(ProcessedEvent.event_id == OutboxEvent.id, ProcessedEvent.consumer == CONSUMER),
                ~exists().where(DeadLetterEvent.event_id == OutboxEvent.id, DeadLetterEvent.consumer == CONSUMER),
            ).order_by(OutboxEvent.created_at).limit(limit))).all()
    applied = 0
    for _, topic, payload in rows:
        outcome = await handle_message(session_factory, topic, payload, max_attempts=2, backoff=lambda attempt: 0.5)
        applied += outcome == "processed"
    if applied:
        logger.info("reconciled stranded events", extra={"applied": applied})
    return applied


async def reconcile_loop(session_factory, interval_seconds: int = 60) -> None:
    while True:
        try:
            await reconcile_outbox(session_factory)
        except Exception:  # noqa: BLE001 - the sweep must never stop the consumer
            logger.warning("outbox reconciliation failed")
        await asyncio.sleep(interval_seconds)
