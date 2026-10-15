"""At-least-once dispatch. Consumers must deduplicate the stable event_id.

A crash after Kafka acknowledges and before SQL commits may deliver twice.
No external actions are performed by this dispatcher. Log delivery is explicitly
recorded as local logging, never represented as broker acknowledgement.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.json_values import json_value
from app.core.observability import request_id_var
from app.events.topics import ALL_TOPICS
from app.models.outbox import OutboxEvent

logger = logging.getLogger("app.events.outbox")


class TransactionalEventPublisher:
    def __init__(self, session):
        self.session = session

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        if topic not in ALL_TOPICS or not key or len(key) > 100:
            raise ValueError("Invalid domain event contract")
        event_id = uuid4()
        self.session.add(OutboxEvent(
            id=event_id, topic=topic, key=key,
            payload=json_value({"schema_version": 1, "event_id": str(event_id),
                                "correlation_id": request_id_var.get() or str(event_id),
                                "occurred_at": datetime.now(timezone.utc).isoformat(), "data": payload}),
        ))


async def dispatch_batch(session_factory, publisher, *, limit: int = 20, event_ids: set[UUID] | None = None) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError("Invalid dispatch limit")
    summary = {"delivered": 0, "failed": 0}
    # SKIP LOCKED allows multiple dispatchers without duplicate concurrent claims.
    async with session_factory() as session:
        async with session.begin():
            now = datetime.now(timezone.utc)
            statement = select(OutboxEvent).where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= now)
            if event_ids is not None:
                statement = statement.where(OutboxEvent.id.in_(event_ids))
            rows = (await session.execute(
                statement
                .order_by(OutboxEvent.created_at, OutboxEvent.id).limit(limit).with_for_update(skip_locked=True)
            )).scalars().all()
            for event in rows:
                event.attempts += 1
                try:
                    # Reliable publish propagates failures instead of swallowing them.
                    await asyncio.wait_for(publisher.publish_reliably(event.topic, event.key, event.payload), timeout=6)
                except Exception:
                    event.available_at = now + timedelta(seconds=min(300, 2 ** min(event.attempts, 8)))
                    summary["failed"] += 1
                    logger.warning("Outbox delivery deferred", extra={"event_id": str(event.id), "topic": event.topic})
                else:
                    event.published_at = datetime.now(timezone.utc)
                    event.delivery_mode = publisher.delivery_mode
                    summary["delivered"] += 1
    return summary


async def dispatch_loop(session_factory, publisher):
    while True:
        try:
            await dispatch_batch(session_factory, publisher)
        except Exception:
            logger.warning("Outbox dispatcher unavailable")
        await asyncio.sleep(5)
