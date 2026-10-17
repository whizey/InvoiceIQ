"""Effects of consumed events, applied at most once per event_id.

The dedupe marker and the effect share one transaction: if the effect fails the marker
rolls back too, so a retry starts clean; if the event is redelivered after success, the
marker insert conflicts and nothing happens. Effects stay inside this database and never
contact customers or other systems.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.events import topics
from app.models.event_effects import ApprovalAlert, CaseRiskLatest, DeadLetterEvent, ProcessedEvent

logger = logging.getLogger("app.events.consumer")

CONSUMER = "effects-v1"
HIGH_RISK_LEVELS = {"HIGH", "CRITICAL"}
MAX_ATTEMPTS = 3


class PoisonEvent(Exception):
    """The message can never succeed (unreadable or malformed); do not retry it."""


def _envelope(raw: bytes | str | dict) -> dict:
    try:
        body = raw if isinstance(raw, dict) else json.loads(raw)
        event_id = uuid.UUID(str(body["event_id"]))
        data = body["data"]
        occurred_at = datetime.fromisoformat(body["occurred_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PoisonEvent("Malformed event envelope") from exc
    if not isinstance(data, dict):
        raise PoisonEvent("Event data must be an object")
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return {"event_id": event_id, "data": data, "occurred_at": occurred_at, "body": body}


def _case_id(data: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(data["case_id"]))
    except (KeyError, ValueError) as exc:
        raise PoisonEvent("Event has no valid case_id") from exc


async def _apply_risk_scored(session, event: dict) -> None:
    data, case_id = event["data"], _case_id(event["data"])
    level = str(data.get("risk_level") or "").upper()
    if not level:
        raise PoisonEvent("risk.scored without risk_level")
    current = await session.get(CaseRiskLatest, case_id)
    if current is not None and current.occurred_at >= event["occurred_at"]:
        return  # an older event arriving late must not overwrite a newer score
    values = dict(risk_level=level, risk_score=data.get("risk_score"),
                  recovery_probability=data.get("recovery_probability"), drivers=data.get("top_drivers"),
                  event_id=event["event_id"], occurred_at=event["occurred_at"])
    statement = insert(CaseRiskLatest).values(case_id=case_id, **values)
    await session.execute(statement.on_conflict_do_update(index_elements=["case_id"], set_=values))


def _workspace_id(data: dict) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(data["workspace_id"])) if data.get("workspace_id") else None
    except ValueError as exc:
        raise PoisonEvent("Event has an invalid workspace_id") from exc


async def _apply_review_requested(session, event: dict) -> None:
    data = event["data"]
    await session.execute(insert(ApprovalAlert).values(
        event_id=event["event_id"], case_id=_case_id(data), action_id=str(data.get("action_id") or "") or None,
        action_type=data.get("action_type"), reason=data.get("reason"),
        workspace_id=_workspace_id(data), visibility_scope=data.get("visibility_scope"),
        subject=(data.get("subject") or None) and str(data["subject"])[:240],
    ).on_conflict_do_nothing())


async def _apply_review_resolved(session, event: dict) -> None:
    """A recorded decision closes the open alerts for that case in the same scope."""
    data = event["data"]
    decision = str(data.get("decision") or "").upper()
    if not decision:
        raise PoisonEvent("review.resolved without a decision")
    scope = [ApprovalAlert.case_id == _case_id(data), ApprovalAlert.status == "OPEN"]
    workspace_id = _workspace_id(data)
    if workspace_id is not None:
        scope += [ApprovalAlert.workspace_id == workspace_id,
                  ApprovalAlert.visibility_scope == data.get("visibility_scope")]
    await session.execute(update(ApprovalAlert).where(*scope).values(
        status="RESOLVED", resolved_decision=decision, resolved_at=event["occurred_at"]))


HANDLERS = {topics.RISK_SCORED: _apply_risk_scored, topics.REVIEW_REQUESTED: _apply_review_requested,
            topics.REVIEW_RESOLVED: _apply_review_resolved}


async def process_event(session, topic: str, raw) -> str:
    """Apply one event. Returns processed, duplicate or ignored. The caller commits."""
    handler = HANDLERS.get(topic)
    if handler is None:
        return "ignored"
    event = _envelope(raw)
    claimed = await session.execute(
        insert(ProcessedEvent).values(consumer=CONSUMER, event_id=event["event_id"], topic=topic)
        .on_conflict_do_nothing().returning(ProcessedEvent.event_id))
    if claimed.scalar_one_or_none() is None:
        return "duplicate"
    await handler(session, event)
    return "processed"


async def handle_message(session_factory, topic: str, raw, *, dead_letter_publisher=None,
                         max_attempts: int = MAX_ATTEMPTS, backoff=lambda attempt: min(30, 2 ** attempt)) -> str:
    """Process with retries; park the event on the dead-letter table and topic if it keeps failing."""
    attempts, last_error = 0, ""
    while attempts < max_attempts:
        attempts += 1
        try:
            async with session_factory() as session:
                async with session.begin():
                    return await process_event(session, topic, raw)
        except PoisonEvent as exc:
            last_error = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 - any failure is retried, then parked
            last_error = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("Event handling failed", extra={"topic": topic, "attempt": attempts})
            if attempts < max_attempts:
                await asyncio.sleep(backoff(attempts))
    await _dead_letter(session_factory, topic, raw, last_error, attempts, dead_letter_publisher)
    return "dead_lettered"


async def _dead_letter(session_factory, topic, raw, error, attempts, publisher) -> None:
    try:
        body = raw if isinstance(raw, dict) else json.loads(raw)
        event_id = uuid.UUID(str(body["event_id"]))
    except (KeyError, TypeError, ValueError):
        body, event_id = {"unreadable": True}, uuid.uuid5(uuid.NAMESPACE_OID, repr(raw))
    async with session_factory() as session:
        async with session.begin():
            await session.execute(insert(DeadLetterEvent).values(
                id=uuid.uuid4(), consumer=CONSUMER, event_id=event_id, topic=topic, payload=body,
                error=error, attempts=attempts).on_conflict_do_nothing())
    if publisher is not None:
        try:
            await publisher.publish(topics.dead_letter_topic(topic), str(event_id), {**body, "dead_letter_error": error})
        except Exception:  # noqa: BLE001 - the table already holds the record
            logger.warning("Dead-letter publish failed", extra={"topic": topic})


async def high_risk_cases(session, limit: int = 50) -> list[CaseRiskLatest]:
    rows = await session.execute(select(CaseRiskLatest).where(CaseRiskLatest.risk_level.in_(HIGH_RISK_LEVELS))
                                 .order_by(CaseRiskLatest.risk_score.desc().nullslast()).limit(limit))
    return list(rows.scalars())


async def forget_case(session, case_id: uuid.UUID) -> None:
    await session.execute(delete(CaseRiskLatest).where(CaseRiskLatest.case_id == case_id))
