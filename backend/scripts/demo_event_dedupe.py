"""Show that the consumer applies one event once, using only the disposable test DB.

Run from backend after provisioning the guarded test database:
    source .env.test && python -m scripts.demo_event_dedupe
"""

import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select

from scripts.test_database_safety import verify


async def demo() -> None:
    from app.core.db import async_session_factory, engine
    from app.events.handlers import CONSUMER, handle_message
    from app.events.topics import REVIEW_REQUESTED
    from app.models.event_effects import ApprovalAlert, ProcessedEvent

    event_id, case_id = uuid4(), uuid4()
    event = {
        "schema_version": 1,
        "event_id": str(event_id),
        "correlation_id": str(event_id),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": {"case_id": str(case_id), "action_type": "SEND_EMAIL", "reason": "Demo review"},
    }
    try:
        first = await handle_message(async_session_factory, REVIEW_REQUESTED, event)
        second = await handle_message(async_session_factory, REVIEW_REQUESTED, event)
        async with async_session_factory() as session:
            alerts = await session.scalar(select(func.count()).select_from(ApprovalAlert).where(
                ApprovalAlert.event_id == event_id))
            markers = await session.scalar(select(func.count()).select_from(ProcessedEvent).where(
                ProcessedEvent.consumer == CONSUMER, ProcessedEvent.event_id == event_id))
        assert (first, second, alerts, markers) == ("processed", "duplicate", 1, 1)
        print(f"first delivery: {first}\nsecond delivery: {second}\napproval alerts: {alerts}\ndedupe markers: {markers}")
    finally:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ApprovalAlert).where(ApprovalAlert.event_id == event_id))
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.consumer == CONSUMER,
                    ProcessedEvent.event_id == event_id))
        await engine.dispose()


def main() -> None:
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        raise SystemExit("TEST_DATABASE_URL is required; provision the disposable test database first")
    verify(test_url)
    os.environ["DATABASE_URL"] = test_url
    asyncio.run(demo())


if __name__ == "__main__":
    main()
