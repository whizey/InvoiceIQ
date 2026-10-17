"""Initialize only an empty, explicitly designated portfolio sample database.

Used as a deployment preflight. Existing unmarked schemas are never seeded.
"""

import asyncio
import os
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url


async def database_state():
    from app.core.db import engine

    try:
        async with engine.connect() as connection:
            tables = (await connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ))).scalars().all()
            if not tables:
                return "empty"
            if "dataset_imports" not in tables:
                return "unmarked"
            marker = await connection.scalar(text(
                "SELECT source_sha256 FROM dataset_imports WHERE source = 'synthetic_portfolio_sample_v1'"
            ))
            from app.seed.synthetic_sample import source_digest

            return "marked" if marker == source_digest() else "unmarked"
    finally:
        await engine.dispose()


async def verify_scope():
    from app.core.db import async_session_factory, engine
    from app.seed.synthetic_sample import synthetic_sample_scope

    try:
        async with async_session_factory() as db:
            await synthetic_sample_scope(db)
    finally:
        await engine.dispose()


async def correct_decision_labels():
    from app.core.db import engine
    from app.seed.synthetic_sample import correct_fixture_decision_provenance

    try:
        await correct_fixture_decision_provenance()
    finally:
        await engine.dispose()


def main():
    if os.getenv("SAMPLE_SYNTHETIC", "").lower() != "true" or os.getenv("PUBLIC_READ_ONLY", "").lower() != "true":
        raise RuntimeError("Synthetic sample initialization requires both safety flags")
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("An explicit sample DATABASE_URL is required")
    url = make_url(raw)
    if url.database not in {"invoiceiq_sample", "invoiceiq_sample_test"}:
        raise RuntimeError("Refusing to initialize a database outside the dedicated sample names")
    if url.database == "invoiceiq_sample_test":
        from scripts.test_database_safety import verify
        verify(raw)

    state = asyncio.run(database_state())
    if state == "unmarked":
        raise RuntimeError("Existing database lacks verified synthetic provenance; refusing to modify it")
    if state == "empty":
        for command in ([sys.executable, "-m", "alembic", "upgrade", "head"],
                        [sys.executable, "-m", "app.seed.run"],
                        [sys.executable, "-m", "app.seed.synthetic_sample"]):
            subprocess.run(command, check=True)
    else:
        asyncio.run(correct_decision_labels())
        asyncio.run(verify_scope())
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    asyncio.run(verify_scope())
    print("Dedicated synthetic sample database verified")


if __name__ == "__main__":
    main()
