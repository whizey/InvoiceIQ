"""Create, migrate, and seed one new database in the verified tmpfs test container."""

import asyncio
import json
import os
import subprocess
import sys

import asyncpg
from sqlalchemy.engine import make_url

from scripts.test_database_safety import CONTAINER, docker, inspect_container, verify

SAMPLE = "invoiceiq_sample_test"


async def exists(raw, name):
    url = make_url(raw)
    conn = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                                 password=url.password, database=url.database, timeout=10)
    try:
        async with conn.transaction(readonly=True):
            return await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name) is not None
    finally:
        await conn.close()


def main():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        raise RuntimeError("TEST_DATABASE_URL is required; load the private disposable test environment")
    verify(raw)
    base = make_url(raw)
    if base.database != "invoiceiq_test":
        raise RuntimeError("The source connection must be the verified primary test database")
    if asyncio.run(exists(raw, SAMPLE)):
        raise RuntimeError("Synthetic sample database already exists; refusing to replace or reseed it")
    container_id = inspect_container(json.loads(docker("inspect", CONTAINER))[0])
    subprocess.run(["docker", "exec", container_id, "createdb", "-U", base.username, SAMPLE], check=True)
    sample_url = base.set(database=SAMPLE).render_as_string(hide_password=False)
    verify(sample_url)
    env = dict(os.environ, DATABASE_URL=sample_url, PUBLIC_READ_ONLY="false",
               SAMPLE_SYNTHETIC="false", SCHEDULER_ENABLED="false",
               OUTBOX_DISPATCH_ENABLED="false", GOOGLE_API_KEY="", OPENAI_API_KEY="",
               GROQ_API_KEY="", RESEND_API_KEY="", SARVAM_API_KEY="", REDIS_URL="",
               KAFKA_BOOTSTRAP_SERVERS="")
    for command in ([sys.executable, "-m", "alembic", "upgrade", "head"],
                    [sys.executable, "-m", "app.seed.run"],
                    [sys.executable, "-m", "app.seed.synthetic_sample"]):
        subprocess.run(command, env=env, check=True)
    print("Provisioned one isolated synthetic sample database")


if __name__ == "__main__":
    main()
