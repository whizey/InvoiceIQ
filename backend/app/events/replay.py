"""Replay dead-lettered events once the cause of their failure has been fixed.

    python -m app.events.replay --dry-run
    python -m app.events.replay --topic review.requested --limit 50
    python -m app.events.replay --event-id <uuid>

Each event goes back through the same idempotent handlers, so replaying one that was already applied is a no-op.
Successful replays are stamped with `replayed_at` (the row is kept for audit). Unreadable messages cannot be replayed
and are reported.
"""
import argparse
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.db import async_session_factory
from app.events.handlers import CONSUMER, handle_message
from app.models.event_effects import DeadLetterEvent


async def replay_dead_letters(session_factory, *, topic: str | None = None, event_id: uuid.UUID | None = None,
                              limit: int = 100, dry_run: bool = False) -> dict:
    async with session_factory() as session:
        query = select(DeadLetterEvent).where(DeadLetterEvent.consumer == CONSUMER, DeadLetterEvent.replayed_at.is_(None))
        if topic:
            query = query.where(DeadLetterEvent.topic == topic)
        if event_id:
            query = query.where(DeadLetterEvent.event_id == event_id)
        rows = (await session.scalars(query.order_by(DeadLetterEvent.created_at).limit(limit))).all()
    summary = {"found": len(rows), "replayed": 0, "still_failing": 0, "unreadable": 0, "dry_run": dry_run}
    for row in rows:
        if not isinstance(row.payload, dict) or row.payload.get("unreadable"):
            summary["unreadable"] += 1
            continue
        if dry_run:
            continue
        payload = {key: value for key, value in row.payload.items() if key != "dead_letter_error"}
        outcome = await handle_message(session_factory, row.topic, payload, max_attempts=2, backoff=lambda attempt: 1)
        if outcome in {"processed", "duplicate"}:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(update(DeadLetterEvent).where(DeadLetterEvent.id == row.id)
                                          .values(replayed_at=datetime.now(timezone.utc)))
            summary["replayed"] += 1
        else:
            summary["still_failing"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay dead-lettered events")
    parser.add_argument("--topic")
    parser.add_argument("--event-id", type=uuid.UUID)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true", help="list what would be replayed without applying anything")
    args = parser.parse_args()
    print(asyncio.run(replay_dead_letters(async_session_factory, topic=args.topic, event_id=args.event_id,
                                          limit=args.limit, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
