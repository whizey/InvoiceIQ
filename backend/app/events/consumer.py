"""Standalone demo consumer — proves Kafka messages are actually flowing.

Not part of the FastAPI app's request path. V1 has no long-running consumer
driving the app itself (see docs/architecture.md decision #2) — this is
purely to demonstrate that the publish side of the outbox pattern works.

Run with: python -m app.events.consumer
"""

import asyncio
import json

from aiokafka import AIOKafkaConsumer

from app.core.config import settings
from app.events.topics import ALL_TOPICS


async def main() -> None:
    if not settings.kafka_bootstrap_servers:
        print("KAFKA_BOOTSTRAP_SERVERS is not set — nothing to consume.", flush=True)
        return

    consumer = AIOKafkaConsumer(
        *ALL_TOPICS,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="demo-consumer",
        auto_offset_reset="earliest",
    )
    await consumer.start()
    print(f"Listening on {settings.kafka_bootstrap_servers}, topics: {ALL_TOPICS}", flush=True)
    print("(Ctrl+C to stop)\n", flush=True)
    try:
        async for message in consumer:
            key = message.key.decode("utf-8") if message.key else None
            payload = json.loads(message.value.decode("utf-8"))
            print(f"[{message.topic}] key={key} {json.dumps(payload)}", flush=True)
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(main())
