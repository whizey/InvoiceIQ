"""Sign-in throttling: lockout, backoff, isolation between accounts and no account enumeration."""
import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.db import async_session_factory, engine
from app.core.login_throttle import (BASE_LOCKOUT_SECONDS, FAILURES_BEFORE_LOCKOUT, MAX_LOCKOUT_SECONDS,
                                     WINDOW_SECONDS, LoginThrottle)
from app.main import app
from app.models.portal import PortalUser, PortalWorkspace


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fail(throttle, key, times):
    for _ in range(times):
        throttle.check(key)
        throttle.record_failure(key)


def test_locks_after_repeated_failures_then_releases_and_backs_off():
    clock = Clock()
    throttle = LoginThrottle(clock)
    fail(throttle, "a@example.invalid", FAILURES_BEFORE_LOCKOUT)
    with pytest.raises(HTTPException) as locked:
        throttle.check("a@example.invalid")
    assert locked.value.status_code == 429 and int(locked.value.headers["Retry-After"]) >= BASE_LOCKOUT_SECONDS
    clock.now += BASE_LOCKOUT_SECONDS + 1
    throttle.check("a@example.invalid")                       # released
    throttle.record_failure("a@example.invalid")              # one more failure: a longer lockout
    clock.now += BASE_LOCKOUT_SECONDS + 1
    with pytest.raises(HTTPException):
        throttle.check("a@example.invalid")
    clock.now += MAX_LOCKOUT_SECONDS * 2
    for _ in range(20):                                       # the lockout never exceeds the cap
        throttle.record_failure("a@example.invalid")
    with pytest.raises(HTTPException) as capped:
        throttle.check("a@example.invalid")
    assert int(capped.value.headers["Retry-After"]) <= MAX_LOCKOUT_SECONDS + 1


def test_accounts_are_independent_and_success_resets_the_streak():
    throttle = LoginThrottle(Clock())
    fail(throttle, "a@example.invalid", FAILURES_BEFORE_LOCKOUT - 1)
    throttle.record_success("a@example.invalid")
    fail(throttle, "a@example.invalid", FAILURES_BEFORE_LOCKOUT - 1)   # would lock if the streak had not reset
    throttle.check("a@example.invalid")
    fail(throttle, "b@example.invalid", FAILURES_BEFORE_LOCKOUT)
    with pytest.raises(HTTPException):
        throttle.check("b@example.invalid")
    throttle.check("a@example.invalid")                        # b's lockout does not affect a


def test_an_old_streak_is_forgiven_and_memory_is_bounded():
    clock = Clock()
    throttle = LoginThrottle(clock)
    fail(throttle, "a@example.invalid", FAILURES_BEFORE_LOCKOUT - 1)
    clock.now += WINDOW_SECONDS + 5
    throttle.record_failure("a@example.invalid")               # outside the window: counts as the first
    throttle.check("a@example.invalid")
    from app.core import login_throttle as module
    small = LoginThrottle(clock)
    for i in range(module.MAX_TRACKED_ACCOUNTS + 50):
        small.record_failure(f"u{i}@example.invalid")
    assert len(small._entries) <= module.MAX_TRACKED_ACCOUNTS


@pytest.mark.asyncio
async def test_sign_in_locks_out_guessing_without_revealing_which_accounts_exist(monkeypatch):
    from app.api import portal
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    monkeypatch.setattr(portal, "login_throttle", LoginThrottle())
    await engine.dispose()
    tag, password = uuid.uuid4().hex, "A-strong-unique-long-password-37"
    email = f"lock-{tag}@example.invalid"
    ids = {}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            reg = await client.post("/portal/register", json={
                "email": email, "full_name": "Lock Owner", "company": "Lock Co", "password": password})
            assert reg.status_code == 201, reg.text
            me = (await client.get("/portal/me", headers={"Authorization": f"Bearer {reg.json()['token']}"})).json()
            ids = {"workspace": uuid.UUID(me["workspace_id"]), "user": uuid.UUID(me["user_id"])}

            for _ in range(FAILURES_BEFORE_LOCKOUT):
                bad = await client.post("/portal/login", json={"email": email, "password": "wrong-password-123"})
                assert bad.status_code == 401
            locked = await client.post("/portal/login", json={"email": email, "password": password})   # even the right one
            assert locked.status_code == 429 and "Retry-After" in locked.headers
            assert "Try again in" in locked.json()["detail"]

            # An address that does not exist behaves identically: no way to tell accounts apart.
            ghost = f"ghost-{tag}@example.invalid"
            codes = [(await client.post("/portal/login", json={"email": ghost, "password": "x" * 14})).status_code
                     for _ in range(FAILURES_BEFORE_LOCKOUT + 1)]
            assert codes == [401] * FAILURES_BEFORE_LOCKOUT + [429]

            # Other accounts are untouched, and a good password works before any lockout.
            other = await client.post("/portal/register", json={
                "email": f"other-{tag}@example.invalid", "full_name": "Other Owner", "company": "Other Co", "password": password})
            assert other.status_code == 201
            ok = await client.post("/portal/login", json={"email": f"other-{tag}@example.invalid", "password": password})
            assert ok.status_code == 200
            other_me = (await client.get("/portal/me", headers={"Authorization": f"Bearer {other.json()['token']}"})).json()
            ids["workspace2"], ids["user2"] = uuid.UUID(other_me["workspace_id"]), uuid.UUID(other_me["user_id"])
    finally:
        async with async_session_factory() as db:
            workspaces = [v for k, v in ids.items() if k.startswith("workspace")]
            users = [v for k, v in ids.items() if k.startswith("user")]
            if workspaces:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_(workspaces)))
            if users:
                await db.execute(delete(PortalUser).where(PortalUser.id.in_(users)))
            await db.commit()
        await engine.dispose()


def test_registration_limit_is_a_sliding_window():
    from app.core.login_throttle import SlidingWindowLimit
    clock = Clock()
    limit = SlidingWindowLimit(3, 60, clock)
    for _ in range(3):
        limit.check()
    with pytest.raises(HTTPException) as blocked:
        limit.check()
    assert blocked.value.status_code == 429
    assert int(blocked.value.headers["Retry-After"]) > 0
    clock.now += 61   # the window has passed
    limit.check()
