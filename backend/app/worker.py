"""Isolated Phase 1 worker entrypoint, reusing the application's background jobs.

python -m app.worker
WORKER_DATABASE_URL must equal the explicitly configured PHASE1_TEST_DATABASE_URL.
No development/production database or outbound provider is permitted here.
"""
import asyncio
import logging
import os
import signal

from sqlalchemy.engine import make_url


def configure_database():
    configured = os.environ.get('WORKER_DATABASE_URL')
    expected = os.environ.get('PHASE1_TEST_DATABASE_URL')
    if not configured or not expected or os.environ.get('ENV') != 'test':
        raise RuntimeError('Phase 1 worker requires explicit isolated test database configuration')
    url = make_url(configured)
    if (url != make_url(expected) or not url.drivername.startswith('postgresql')
            or not url.database or not url.database.endswith('_test')):
        raise RuntimeError('Worker database does not match the isolated Phase 1 test database')
    os.environ['DATABASE_URL'] = configured
    print('WORKER_DATABASE_IDENTITY:', {'host': url.host, 'port': url.port,
        'database': url.database, 'environment': 'phase1-worker-test'}, flush=True)


async def main():
    configure_database()
    from app.main import app, lifespan
    from app.core.config import settings
    from app.core.db import check_db_connection, engine
    from app.events import get_publisher
    from app.ml.champion_serving import verified_contract, _load_verified_model
    import redis.asyncio as redis
    if (os.getenv('PUBLIC_READ_ONLY', '').lower() == 'true' or not settings.redis_url
            or not settings.kafka_bootstrap_servers or not settings.scheduler_enabled
            or not settings.outbox_dispatch_enabled):
        raise RuntimeError('Worker requires Redis, Kafka and both background jobs enabled; public mode is prohibited')
    if settings.resend_api_key or settings.sarvam_api_key:
        raise RuntimeError('Outbound provider configuration is prohibited in the Phase 1 worker')
    from app.agents.llm_client import configured_llm
    if configured_llm()[0] != 'fallback':
        raise RuntimeError('External LLM calls are prohibited in the isolated Phase 1 worker')
    await check_db_connection()
    client = redis.from_url(settings.redis_url)
    publisher = get_publisher()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    try:
        if not await client.ping():
            raise RuntimeError('Worker Redis readiness failed')
        await publisher._get_producer()  # Real Kafka initialization before readiness.
        contract = verified_contract()
        _load_verified_model(contract['version'])
        async with lifespan(app):
            logging.getLogger('app.worker').info('worker ready: database, redis, kafka, champion initialized; jobs=recovery-scheduler,event-outbox')
            await stopped.wait()
        logging.getLogger('app.worker').info('worker shutdown complete')
    finally:
        await publisher.close()
        await client.aclose()
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
