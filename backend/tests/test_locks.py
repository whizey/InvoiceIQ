import asyncio

import pytest

from app.core.locks import LockAcquisitionError, acquire_lock

pytestmark = pytest.mark.asyncio


async def test_lock_can_be_acquired_and_released():
    async with acquire_lock("test-lock-basic"):
        pass
    # Freed on exit — acquiring again immediately must succeed.
    async with acquire_lock("test-lock-basic"):
        pass


async def test_concurrent_acquire_of_same_lock_is_rejected():
    events: list[str] = []

    async def holder():
        async with acquire_lock("test-lock-contended"):
            events.append("holder-acquired")
            await asyncio.sleep(0.2)
        events.append("holder-released")

    async def contender():
        await asyncio.sleep(0.05)  # let the holder acquire first
        try:
            async with acquire_lock("test-lock-contended"):
                events.append("contender-acquired")  # must never happen
        except LockAcquisitionError:
            events.append("contender-rejected")

    await asyncio.gather(holder(), contender())

    assert events[0] == "holder-acquired"
    assert "contender-rejected" in events
    assert "contender-acquired" not in events


async def test_lock_is_released_even_if_the_body_raises():
    with pytest.raises(ValueError):
        async with acquire_lock("test-lock-exception"):
            raise ValueError("boom")

    # Must be free again despite the exception — no permanent deadlock.
    async with acquire_lock("test-lock-exception"):
        pass


async def test_different_keys_do_not_contend():
    async with acquire_lock("test-lock-a"):
        async with acquire_lock("test-lock-b"):
            pass  # unrelated keys must not block each other
