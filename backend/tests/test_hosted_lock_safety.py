import pytest

from app.core.config import settings
from app.core.locks import LockAcquisitionError, acquire_lock


@pytest.mark.asyncio
async def test_hosted_writes_must_not_fall_back_to_per_process_lock(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "redis_url", None)
    with pytest.raises(LockAcquisitionError, match="Distributed lock"):
        async with acquire_lock("some-case"):
            pytest.fail("Hosted write unexpectedly continued without distributed lock")


@pytest.mark.asyncio
async def test_local_demo_retains_existing_lock_behavior(monkeypatch):
    monkeypatch.setattr(settings, "env", "test")
    monkeypatch.setattr(settings, "redis_url", None)
    async with acquire_lock("local-case"):
        with pytest.raises(LockAcquisitionError):
            async with acquire_lock("local-case"):
                pytest.fail("Second caller acquired same lock")
