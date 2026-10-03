"""Distributed locking / idempotency guard.

Prevents two concurrent triggers of the same operation — e.g. two
overlapping POST /recovery-cases/{id}/run calls for the same case, or two
overlapping detect-overdue sweeps — from racing and double-executing
actions. Falls back to an in-process lock when REDIS_URL isn't configured,
the same auto-fallback pattern as the LLM client and Kafka publisher. The
fallback only guards within this one process; it does not coordinate
across multiple app instances the way real Redis does.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from app.core.config import settings

_local_locks: dict[str, asyncio.Lock] = {}

DEFAULT_LOCK_TTL_SECONDS = 60


class LockAcquisitionError(Exception):
    """Raised when a lock is already held by another in-flight operation."""


REDIS_CONNECT_TIMEOUT_SECONDS = 2.0
REDIS_OPERATION_TIMEOUT_SECONDS = 2.0


@contextlib.asynccontextmanager
async def _local_lock(key: str, ttl_seconds: int) -> AsyncIterator[None]:
    """In-process fallback.

    ttl_seconds is accepted and deliberately unused: an asyncio.Lock has no
    expiry, and a lock held by a coroutine in this process cannot be stolen
    by a timeout the way a Redis lock can. Callers that pass a long TTL get
    a lock held for exactly as long as the work takes, which is stricter,
    not looser. Taking the argument keeps one signature for both paths.
    """
    lock = _local_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        raise LockAcquisitionError(f"Lock '{key}' is already held")
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextlib.asynccontextmanager
async def acquire_lock(key: str, ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS) -> AsyncIterator[None]:
    if not settings.redis_url:
        async with _local_lock(key, ttl_seconds):
            yield
        return

    import redis.asyncio as aioredis

    # Timeouts matter more here than anywhere else in the app: acquire_lock
    # wraps the recovery-cycle endpoint and the scheduler sweep, so a Redis
    # that accepts TCP but never answers used to hang both forever with no
    # bound. docker-compose starts Redis with `condition: service_started`
    # rather than service_healthy, so "up but not answering" is a state
    # that really occurs.
    client = aioredis.from_url(
        settings.redis_url,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_OPERATION_TIMEOUT_SECONDS,
    )
    lock = client.lock(f"lock:{key}", timeout=ttl_seconds, blocking=False)

    try:
        acquired = await lock.acquire()
    except LockAcquisitionError:
        raise
    except Exception:
        # Redis is configured but unreachable. The module docstring promises
        # "the same auto-fallback pattern as the LLM client and Kafka
        # publisher", and those fall back on FAILURE, not merely on absent
        # config -- this path previously raised and surfaced as a 500 on
        # POST /recovery-cases/{id}/run. Degrading to the in-process lock
        # keeps the endpoint serving.
        #
        # Stated plainly because it is a real weakening: while Redis is
        # down, concurrency is only guarded within this process, so two app
        # replicas could each run a cycle for the same case. That is the
        # same guarantee the app has whenever REDIS_URL is unset, and it is
        # a better failure mode than refusing all traffic.
        with contextlib.suppress(Exception):
            await client.aclose()
        async with _local_lock(key, ttl_seconds):
            yield
        return

    try:
        if not acquired:
            raise LockAcquisitionError(f"Lock '{key}' is already held")
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await lock.release()
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
