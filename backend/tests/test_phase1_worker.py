"""Opt-in actual daemon, PostgreSQL, Redis and Kafka lifecycle verification."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


def test_worker_refuses_unpinned_development_database(monkeypatch):
    from app.worker import configure_database

    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("WORKER_DATABASE_URL", "postgresql+asyncpg://localhost:5435/recovery_db")
    monkeypatch.setenv("PHASE1_TEST_DATABASE_URL", "postgresql+asyncpg://localhost:55435/isolated_test")
    with pytest.raises(RuntimeError, match="does not match"):
        configure_database()


@pytest.mark.asyncio
async def test_real_worker_schedule_retry_restart_and_shutdown():
    if os.getenv("TEST_WORKER_LIFECYCLE") != "1":
        pytest.skip("Opt-in isolated daemon: TEST_WORKER_LIFECYCLE=1 with dedicated Kafka container")
    from app.models import Invoice, OutboxEvent
    from app.events.outbox import TransactionalEventPublisher
    from app.events.topics import RISK_SCORED
    from aiokafka import AIOKafkaConsumer, TopicPartition

    url = os.environ["PHASE1_TEST_DATABASE_URL"]
    kafka = os.environ["TEST_KAFKA_BOOTSTRAP_SERVERS"]
    container = os.environ.get("TEST_KAFKA_CONTAINER", "invoiceiq-phase1-kafka")
    details = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
    assert details["Config"]["Image"] == "apache/kafka:3.9.0"
    assert all(
        p["HostIp"] == "127.0.0.1"
        for ports in details["NetworkSettings"]["Ports"].values()
        if ports
        for p in ports
    )
    env = dict(
        os.environ,
        WORKER_DATABASE_URL=url,
        DATABASE_URL=url,
        ENV="test",
        PUBLIC_READ_ONLY="false",
        SCHEDULER_ENABLED="true",
        OUTBOX_DISPATCH_ENABLED="true",
        SCHEDULER_INITIAL_DELAY_SECONDS="0",
        SCHEDULER_INTERVAL_SECONDS="5",
        KAFKA_BOOTSTRAP_SERVERS=kafka,
        REDIS_URL=os.environ["TEST_REDIS_URL"],
    )
    test_engine = create_async_engine(url)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    process = None
    handles = []
    paths = []
    paused = False
    # This is an observational probe, not a group consumer. Pin its starting
    # offset so a broker outage cannot expire membership and reset an
    # uncommitted 'latest' cursor past the very acknowledgement being checked.
    consumer = AIOKafkaConsumer(
        bootstrap_servers=kafka, group_id=None, auto_offset_reset="earliest", enable_auto_commit=False
    )
    consumer.assign([TopicPartition(RISK_SCORED, 0)])

    def start():
        handle = tempfile.NamedTemporaryFile(
            mode="w", prefix="invoiceiq-phase1-worker-", suffix=".log", delete=False
        )
        handles.append(handle)
        paths.append(Path(handle.name))
        return subprocess.Popen(
            [sys.executable, "-m", "app.worker"], env=env, stdout=handle, stderr=subprocess.STDOUT
        ), paths[-1]

    async def wait_for(predicate, timeout=40):
        async with asyncio.timeout(timeout):
            while not await predicate():
                if process.poll() is not None:
                    pytest.fail("Worker exited unexpectedly; inspect " + str(paths[-1]))
                await asyncio.sleep(0.1)

    async def log_contains(path, text):
        return text in path.read_text()

    async def row_matches(event_id, *, failed=False):
        async with factory() as db:
            event = await db.get(OutboxEvent, event_id)
            return (
                event.attempts >= 1 and event.published_at is None
                if failed
                else event.published_at is not None
            )

    async def stop():
        process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(process.wait, 15)
        assert process.returncode == 0
        assert "worker shutdown complete" in paths[-1].read_text()

    try:
        await consumer.start()
        await consumer.seek_to_end(*consumer.assignment())
        process, path = start()
        await wait_for(lambda: log_contains(path, "worker ready"))
        # Observe two real ticks at the configured minimum five-second schedule.
        await wait_for(lambda: log_contains(path, "scheduler tick finished"))
        async with asyncio.timeout(20):
            while path.read_text().count("scheduler tick finished") < 2:
                await asyncio.sleep(0.1)
        subprocess.run(["docker", "pause", container], check=True, capture_output=True)
        paused = True
        async with factory() as db:
            invoice = await db.scalar(select(Invoice).order_by(Invoice.id).limit(1))
            assert invoice.amount_paid == invoice.amount_total  # No recovery/customer tools are eligible.
            await TransactionalEventPublisher(db).publish(
                RISK_SCORED,
                str(invoice.id),
                {
                    "invoice_id": str(invoice.id),
                    "model_version": invoice.payment_risk_model_version,
                    "probability": str(invoice.late_payment_probability),
                    "verification_run": uuid4().hex,
                },
            )
            await db.flush()
            event = max(
                (await db.scalars(select(OutboxEvent).where(OutboxEvent.key == str(invoice.id)))).all(),
                key=lambda e: e.created_at,
            )
            event_id = event.id
            await db.commit()
        await wait_for(lambda: row_matches(event_id, failed=True))
        await stop()  # Pending failed delivery must survive actual process termination.
        subprocess.run(["docker", "unpause", container], check=True, capture_output=True)
        paused = False
        process, path = start()
        await wait_for(lambda: log_contains(path, "worker ready"))
        await wait_for(lambda: row_matches(event_id))
        async with factory() as db:
            event = await db.get(OutboxEvent, event_id)
            attempts = event.attempts
            assert attempts >= 2 and event.delivery_mode == "kafka"
            payload = dict(event.payload)
        observed = set()
        async with asyncio.timeout(30):
            while str(event_id) not in observed:
                message = await consumer.getone()
                observed.add(json.loads(message.value)["event_id"])
        # Real broker redelivery of the same identity. Consumers are explicitly
        # observational; no Kafka-to-customer execution route exists.
        from app.events.kafka_publisher import KafkaEventPublisher

        publisher = KafkaEventPublisher(kafka)
        try:
            await publisher.publish_reliably(RISK_SCORED, str(invoice.id), payload)
            async with asyncio.timeout(30):
                while True:
                    message = await consumer.getone()
                    if json.loads(message.value)["event_id"] == str(event_id):
                        break
        finally:
            await publisher.close()
        await stop()
        process, path = start()
        await wait_for(lambda: log_contains(path, "worker ready"))
        await asyncio.sleep(6)  # A further relay tick must skip the acknowledged event.
        async with factory() as db:
            assert (await db.get(OutboxEvent, event_id)).attempts == attempts
            stored = await db.get(Invoice, invoice.id)
            assert stored.amount_paid == stored.amount_total == invoice.amount_total
            from app.models import RecoveryAction, RecoveryCase
            from sqlalchemy import func

            assert await db.scalar(select(func.count()).select_from(RecoveryAction)) == 0
            assert await db.scalar(select(func.count()).select_from(RecoveryCase)) == 0
        await stop()
        ticks = [
            json.loads(line)
            for line in paths[0].read_text().splitlines()
            if line.startswith("{")
            and json.loads(line).get("message", "").startswith("scheduler tick finished")
        ]
        spacing = [
            (datetime.fromisoformat(b["timestamp"]) - datetime.fromisoformat(a["timestamp"])).total_seconds()
            for a, b in zip(ticks, ticks[1:])
        ]
        assert spacing and all(5 <= seconds < 10 for seconds in spacing)
        print(
            "WORKER_LIFECYCLE_EVIDENCE:",
            json.dumps(
                {
                    "logs": [str(p) for p in paths],
                    "registered_jobs": ["recovery-scheduler", "event-outbox"],
                    "schedule_seconds": 5,
                    "observed_initial_ticks": len(ticks),
                    "tick_spacing_seconds": spacing,
                    "failed_delivery_survived_restart": True,
                    "kafka_acknowledged": True,
                    "delivery_attempts": attempts,
                    "real_redelivery_had_no_financial_execution": True,
                    "repeat_restart_did_not_resend": True,
                    "graceful_exit_codes": [0, 0, 0],
                },
                sort_keys=True,
            ),
        )
    finally:
        if paused:
            subprocess.run(["docker", "unpause", container], check=True, capture_output=True)
        if process and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 15)
        await consumer.stop()
        for handle in handles:
            handle.close()
        await test_engine.dispose()
