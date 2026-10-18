"""Per-account sign-in throttling with exponential lockout.

Keyed by the normalised email, never by IP: behind the web proxy every visitor shares one source address, and a
forwarded address can be spoofed by anyone who calls the API directly. Unknown emails are throttled exactly like
known ones, so the response never reveals whether an account exists. State is per API process (see the hardening
roadmap for a shared limiter when more than one instance runs).
"""
import os
import time
from dataclasses import dataclass

from fastapi import HTTPException

FAILURES_BEFORE_LOCKOUT = 5
WINDOW_SECONDS = 15 * 60
BASE_LOCKOUT_SECONDS = 30
MAX_LOCKOUT_SECONDS = 15 * 60
MAX_TRACKED_ACCOUNTS = 10_000


@dataclass
class _Entry:
    failures: int = 0
    first_failure: float = 0.0
    locked_until: float = 0.0


class LoginThrottle:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def check(self, key: str) -> None:
        """Raise 429 while the account is locked out."""
        entry = self._entries.get(key)
        now = self._clock()
        if entry and entry.locked_until > now:
            wait = int(entry.locked_until - now) + 1
            raise HTTPException(
                429, f"Too many failed sign-in attempts. Try again in {self._describe(wait)}.",
                headers={"Retry-After": str(wait)})

    def record_failure(self, key: str) -> None:
        now = self._clock()
        self._evict(now)
        entry = self._entries.setdefault(key, _Entry())
        if entry.failures and now - entry.first_failure > WINDOW_SECONDS and entry.locked_until <= now:
            entry.failures = 0   # an old, forgiven streak
        if entry.failures == 0:
            entry.first_failure = now
        entry.failures += 1
        if entry.failures >= FAILURES_BEFORE_LOCKOUT:
            steps = entry.failures - FAILURES_BEFORE_LOCKOUT
            entry.locked_until = now + min(MAX_LOCKOUT_SECONDS, BASE_LOCKOUT_SECONDS * 2 ** steps)

    def record_success(self, key: str) -> None:
        self._entries.pop(key, None)

    def _evict(self, now: float) -> None:
        if len(self._entries) < MAX_TRACKED_ACCOUNTS:
            return
        stale = [k for k, e in self._entries.items()
                 if e.locked_until <= now and now - e.first_failure > WINDOW_SECONDS]
        for key in stale:
            del self._entries[key]
        if len(self._entries) >= MAX_TRACKED_ACCOUNTS:   # still full: drop the oldest streaks
            for key in sorted(self._entries, key=lambda k: self._entries[k].first_failure)[:MAX_TRACKED_ACCOUNTS // 10]:
                del self._entries[key]

    @staticmethod
    def _describe(seconds: int) -> str:
        return f"{seconds} second{'s' if seconds != 1 else ''}" if seconds < 90 else f"{-(-seconds // 60)} minutes"


login_throttle = LoginThrottle()


class SlidingWindowLimit:
    """Allow at most `limit` events per `window` seconds, process-wide.

    Used for account creation, where the source address is not trustworthy behind the web proxy, so a global ceiling
    bounds how fast anyone can create workspaces and rows.
    """

    def __init__(self, limit: int, window: float, clock=time.monotonic):
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: list[float] = []

    def check(self) -> None:
        now = self._clock()
        self._hits = [t for t in self._hits if now - t < self._window]
        if len(self._hits) >= self._limit:
            wait = int(self._window - (now - self._hits[0])) + 1
            raise HTTPException(429, "Too many new accounts right now. Please try again shortly.",
                                headers={"Retry-After": str(wait)})
        self._hits.append(now)


# Generous for real signups, tight enough to stop scripted account creation. Tunable per deployment.
registration_limit = SlidingWindowLimit(int(os.getenv("REGISTRATION_LIMIT_PER_10_MIN", "60")), 600)
