import functools

from app.core.config import settings
from app.events.kafka_publisher import KafkaEventPublisher, kafka_client_options
from app.events.log_publisher import LogEventPublisher
from app.events.publisher import EventPublisher


@functools.lru_cache(maxsize=1)
def get_publisher() -> EventPublisher:
    if settings.kafka_bootstrap_servers:
        return KafkaEventPublisher(settings.kafka_bootstrap_servers, **kafka_client_options(settings))
    return LogEventPublisher()
