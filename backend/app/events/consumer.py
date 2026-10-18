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
from app.events.reconcile import reconcile_loop

logger = logging.getLogger("app.events.consumer")


async def consume_forever(dead_letters) -> None:
    """Connect, consume, and reconnect if the broker goes away or restarts."""
    while True:
        consumer = AIOKafkaConsumer(
            *HANDLERS,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            group_id=CONSUMER,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            **kafka_client_options(settings),
        )
        try:
            await consumer.start()
            print(f"Consuming {sorted(HANDLERS)} on {settings.kafka_bootstrap_servers}", flush=True)
            async for message in consumer:
                outcome = await handle_message(async_session_factory, message.topic, message.value,
                                               dead_letter_publisher=dead_letters)
                await consumer.commit()
                # Payloads can hold private predictions, so only the outcome is logged.
                logger.info("event handled", extra={"topic": message.topic, "outcome": outcome})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - broker restarting or not up yet
            logger.warning("consumer disconnected; retrying", extra={"error": type(exc).__name__})
            await asyncio.sleep(10)
        finally:
            try:
                await consumer.stop()
            except Exception:  # noqa: BLE001
                pass


async def main() -> None:
    if not settings.kafka_bootstrap_servers:
        print("KAFKA_BOOTSTRAP_SERVERS is not set — nothing to consume.", flush=True)
        return
    # The sweep re-applies events a broker with ephemeral storage may have lost; it runs even while the
    # consumer is reconnecting.
    sweep = asyncio.create_task(reconcile_loop(async_session_factory), name="outbox-reconcile")
    try:
        await consume_forever(get_publisher())
    finally:
        sweep.cancel()


if __name__ == "__main__":
    asyncio.run(main())
