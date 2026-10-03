from datetime import date, datetime, timezone


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_today() -> date:
    """Return the current calendar date in UTC."""
    return utc_now().date()