"""Long-running consumer: keeps the high-risk case table and approval alerts up to date.

Delivery from the outbox is at-least-once, so every event is applied once per event_id
(see app/events/handlers.py). Events that keep failing are parked on `<topic>.dlq` and in
the dead_letter_events table. Offsets are committed only after an event is fully handled.

Run with: python -m app.events.consumer
"""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer

from app.core.config import settings
from app.core.db import async_session_factory
from app.events import get_publisher
from app.events.handlers import CONSUMER, HANDLERS, handle_message
from app.events.kafka_publisher import kafka_client_options

logger = logging.getLogger("app.events.consumer")


async def main() -> None:
    if not settings.kafka_bootstrap_servers:
        print("KAFKA_BOOTSTRAP_SERVERS is not set — nothing to consume.", flush=True)
        return

    consumer = AIOKafkaConsumer(
        *HANDLERS,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=CONSUMER,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        **kafka_client_options(settings),
    )
    await consumer.start()
    dead_letters = get_publisher()
    print(f"Consuming {sorted(HANDLERS)} on {settings.kafka_bootstrap_servers} (Ctrl+C to stop)", flush=True)
    try:
        async for message in consumer:
            outcome = await handle_message(async_session_factory, message.topic, message.value,
                                           dead_letter_publisher=dead_letters)
            await consumer.commit()
            # Payloads can hold private predictions, so only the outcome is logged.
            logger.info("event handled", extra={"topic": message.topic, "outcome": outcome})
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
