"""End-to-end health of the event pipeline, computed from the database so it is correct no matter which
process (API, consumer, dispatcher) handled an event and whichever broker, if any, carried it.

`awaiting_consumer` is the true end-to-end lag: events published to Kafka that no consumer has applied yet.
"""
from datetime import datetime, timezone

from sqlalchemy import exists, func, select

from app.events.handlers import CONSUMER, HANDLERS
from app.models.event_effects import DeadLetterEvent, ProcessedEvent
from app.models.outbox import OutboxEvent

KNOWN_TOPICS = set(HANDLERS)


def _age(moment, now) -> float:
    return max(0.0, (now - moment).total_seconds()) if moment else 0.0


async def pipeline_stats(session_factory) -> dict:
    now = datetime.now(timezone.utc)
    handled = OutboxEvent.topic.in_(list(HANDLERS))
    done = exists().where(ProcessedEvent.event_id == OutboxEvent.id, ProcessedEvent.consumer == CONSUMER)
    parked = exists().where(DeadLetterEvent.event_id == OutboxEvent.id, DeadLetterEvent.consumer == CONSUMER)
    async with session_factory() as session:
        pending = (await session.execute(select(func.count(), func.min(OutboxEvent.created_at)).where(
            OutboxEvent.published_at.is_(None)))).one()
        awaiting = (await session.execute(select(func.count(), func.min(OutboxEvent.published_at)).where(
            handled, OutboxEvent.delivery_mode == "kafka", OutboxEvent.published_at.is_not(None), ~done, ~parked))).one()
        letters = (await session.execute(select(DeadLetterEvent.topic, func.count()).where(
            DeadLetterEvent.replayed_at.is_(None)).group_by(DeadLetterEvent.topic))).all()
        processed = await session.scalar(select(func.count()).select_from(ProcessedEvent).where(
            ProcessedEvent.consumer == CONSUMER)) or 0
        replayed = await session.scalar(select(func.count()).select_from(DeadLetterEvent).where(
            DeadLetterEvent.replayed_at.is_not(None))) or 0
    by_topic: dict[str, int] = {}
    for topic, count in letters:   # unknown topic names become "other" so labels stay low-cardinality
        key = topic if topic in KNOWN_TOPICS else "other"
        by_topic[key] = by_topic.get(key, 0) + count
    return {
        "outbox_unpublished": pending[0], "outbox_unpublished_oldest_seconds": _age(pending[1], now),
        "awaiting_consumer": awaiting[0], "awaiting_consumer_oldest_seconds": _age(awaiting[1], now),
        "dead_letters_by_topic": by_topic, "events_processed": processed, "dead_letters_replayed": replayed,
    }


def render_prometheus(stats: dict) -> str:
    lines: list[str] = []

    def gauge(name: str, help_text: str, value, kind: str = "gauge") -> None:
        lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", f"{name} {value}"])

    gauge("invoiceiq_outbox_unpublished_events", "Outbox events not yet published to the broker", stats["outbox_unpublished"])
    gauge("invoiceiq_outbox_unpublished_oldest_seconds", "Age of the oldest unpublished outbox event",
          round(stats["outbox_unpublished_oldest_seconds"], 1))
    gauge("invoiceiq_events_awaiting_consumer", "Events published to Kafka that no consumer has applied yet (end-to-end lag)",
          stats["awaiting_consumer"])
    gauge("invoiceiq_events_awaiting_consumer_oldest_seconds", "Age of the oldest event awaiting its consumer",
          round(stats["awaiting_consumer_oldest_seconds"], 1))
    gauge("invoiceiq_events_processed_total", "Events applied by the consumer group", stats["events_processed"], "counter")
    gauge("invoiceiq_dead_letters_replayed_total", "Dead letters successfully replayed", stats["dead_letters_replayed"], "counter")
    lines.extend(["# HELP invoiceiq_dead_letters Dead-lettered events waiting for inspection or replay",
                  "# TYPE invoiceiq_dead_letters gauge"])
    topics = sorted(stats["dead_letters_by_topic"]) or ["none"]
    for topic in topics:
        lines.append(f'invoiceiq_dead_letters{{topic="{topic}"}} {stats["dead_letters_by_topic"].get(topic, 0)}')
    return "\n".join(lines) + "\n"
