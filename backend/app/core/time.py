from datetime import date, datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar

_historical_date: ContextVar[date | None] = ContextVar("historical_replay_as_of", default=None)


@contextmanager
def historical_as_of(as_of: date):
    """Scope business-date calculations to an explicit historical replay.

    System timestamps remain actual measurements; source dates are never shifted.
    """
    token = _historical_date.set(as_of)
    try:
        yield
    finally:
        _historical_date.reset(token)


def days_since_workflow_action(occurred_at: datetime) -> int:
    # Replay actions happen now, unlike the historical financial facts. Their
    # cooldown uses elapsed execution time rather than a negative historical age.
    today = utc_now().date() if _historical_date.get() is not None else utc_today()
    return max(0, (today - occurred_at.date()).days)


def historical_replay_active() -> bool:
    return _historical_date.get() is not None


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_today() -> date:
    """Return the current calendar date in UTC."""
    return _historical_date.get() or utc_now().date()
