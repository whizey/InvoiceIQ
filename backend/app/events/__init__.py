import functools

from app.core.config import settings
from app.events.kafka_publisher import KafkaEventPublisher
from app.events.log_publisher import LogEventPublisher
from app.events.publisher import EventPublisher


@functools.lru_cache(maxsize=1)
def get_publisher() -> EventPublisher:
    if settings.kafka_bootstrap_servers:
        return KafkaEventPublisher(settings.kafka_bootstrap_servers)
    return LogEventPublisher()
