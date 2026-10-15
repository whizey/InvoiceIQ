import os

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

engine_options = {"echo": False, "pool_pre_ping": True}
if os.environ.get("INVOICEIQ_TEST_NULL_POOL") == "1":
    engine_options["poolclass"] = NullPool
engine = create_async_engine(settings.database_url, **engine_options)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncSession:
    async with async_session_factory() as session:
        if request.method == "GET" and (request.url.path in {"/dashboard/analytics", "/analytics", "/ml/dashboard"} or request.url.path.startswith("/dashboard/preserved")):
            # Authorization also reads this session in public mode. Configure
            # the shared snapshot before its first query, not in the endpoint.
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        scope = getattr(request.state, "analytics_scope", None)
        if scope is not None:
            session.info["analytics_scope"] = scope
        yield session


async def check_db_connection() -> bool:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
