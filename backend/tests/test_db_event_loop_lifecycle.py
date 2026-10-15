"""The test engine is used by fixtures that create separate event loops."""

import asyncio

from sqlalchemy import text

from app.core.db import engine


def test_test_engine_connections_survive_separate_event_loops():
    async def query():
        async with engine.connect() as connection:
            return await connection.scalar(text("SELECT 1"))

    assert asyncio.run(query()) == 1
    assert asyncio.run(query()) == 1
