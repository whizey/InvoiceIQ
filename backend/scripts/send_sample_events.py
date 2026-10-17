"""Publish sample events to the configured Kafka broker, including one sent twice.

Usage: python -m scripts.send_sample_events
Reads KAFKA_BOOTSTRAP_SERVERS (and the optional security settings) like the app does.
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.core.config import settings
from app.events import topics
from app.events.kafka_publisher import kafka_client_options


def envelope(data: dict) -> dict:
    event_id = str(uuid.uuid4())
    return {"schema_version": 1, "event_id": event_id, "correlation_id": event_id,
            "occurred_at": datetime.now(timezone.utc).isoformat(), "data": data}


async def main() -> None:
    options = kafka_client_options(settings)
    wanted = [topics.RISK_SCORED, topics.REVIEW_REQUESTED,
              topics.dead_letter_topic(topics.RISK_SCORED), topics.dead_letter_topic(topics.REVIEW_REQUESTED)]
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers, **options)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [NewTopic(name, 1, 1) for name in wanted if name not in existing]
        if missing:
            await admin.create_topics(missing)
    finally:
        await admin.close()

    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers, **options)
    await producer.start()
    try:
        high_case, low_case, review_case = (str(uuid.uuid4()) for _ in range(3))
        high = envelope({"case_id": high_case, "risk_score": 0.93, "risk_level": "HIGH",
                         "recovery_probability": 0.31, "top_drivers": ["days overdue", "past broken promises"]})
        review = envelope({"case_id": review_case, "action_id": "act-1", "action_type": "SEND_EMAIL",
                           "reason": "Amount above auto-approval limit"})
        sends = [(topics.RISK_SCORED, high), (topics.RISK_SCORED, high),          # the same event twice
                 (topics.RISK_SCORED, envelope({"case_id": low_case, "risk_score": 0.12, "risk_level": "LOW"})),
                 (topics.REVIEW_REQUESTED, review), (topics.REVIEW_REQUESTED, review)]
        for topic, event in sends:
            await producer.send_and_wait(topic, key=event["data"]["case_id"].encode(), value=json.dumps(event).encode())
        await producer.send_and_wait(topics.RISK_SCORED, key=b"bad", value=b"this is not an event")  # goes to the dead letters
    finally:
        await producer.stop()
    print("Sent 6 messages: 1 high-risk case (twice), 1 low-risk case, 1 review request (twice), 1 unreadable message.")


if __name__ == "__main__":
    asyncio.run(main())
