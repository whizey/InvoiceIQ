"""Domain-event publishing abstraction.

Postgres is always written first and is the source of truth; publishing
here happens only after that write is committed, and a publish failure is
logged, never raised — nothing in this app should ever fail because a
notification side-channel is unavailable. Swapping the log-only fallback
for real Kafka is a config change (KAFKA_BOOTSTRAP_SERVERS), not a code
change — see app/events/__init__.py.
"""

from typing import Protocol


class EventPublisher(Protocol):
    async def publish(self, topic: str, key: str, payload: dict) -> None: ...

    async def close(self) -> None: ...
