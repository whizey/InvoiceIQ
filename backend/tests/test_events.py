import pytest

from app.events import get_publisher
from app.events.log_publisher import LogEventPublisher


def test_default_publisher_is_log_fallback_without_kafka_configured():
    # backend/.env doesn't set KAFKA_BOOTSTRAP_SERVERS in dev/test, so the
    # app must run fully without a real Kafka broker.
    publisher = get_publisher()
    assert isinstance(publisher, LogEventPublisher)


def test_get_publisher_is_cached_singleton():
    assert get_publisher() is get_publisher()


@pytest.mark.asyncio
async def test_log_publisher_publish_never_raises():
    publisher = LogEventPublisher()
    await publisher.publish("some.topic", "some-key", {"anything": "goes here"})
    await publisher.close()
