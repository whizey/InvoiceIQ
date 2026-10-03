"""In-process recovery loop — the live replacement for click-to-run.

Calls the same services as the HTTP triggers (`run_detection`,
`run_recovery_cycle`) and the same locks, so a dashboard button and the
scheduler cannot double-execute. Opt-in via SCHEDULER_ENABLED; CI leaves
it off. Escalated cases are skipped — a human owns those; OPEN/MONITORING
keep cycling. Terminal cases are skipped by the graph anyway.
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import select

from app.agents.graph import run_recovery_cycle
from app.core.config import settings
from app.core.db import async_session_factory
from app.core.locks import LockAcquisitionError, acquire_lock
from app.models import RecoveryCase
from app.models.enums import RecoveryCaseStatus
from app.services.risk_engine import run_detection

logger = logging.getLogger("app.scheduler")

_ACTIVE_STATUSES = (RecoveryCaseStatus.OPEN, RecoveryCaseStatus.MONITORING)

# Gemini cycles can exceed the default 60s lock TTL; keep the lock for the
# whole graph invocation so a overlapping click cannot interleave.
_CYCLE_LOCK_TTL_SECONDS = 180

_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def run_scheduler_tick() -> dict:
    """One detect sweep, then one recovery cycle per active case."""
    summary = {
        "invoices_marked_overdue": 0,
        "cases_created": 0,
        "cycles_run": 0,
        "cycles_skipped_locked": 0,
        "cycles_failed": 0,
    }

    try:
        async with acquire_lock("detect-overdue"):
            async with async_session_factory() as session:
                detection = await run_detection(session)
        summary["invoices_marked_overdue"] = len(detection.invoices_marked_overdue)
        summary["cases_created"] = len(detection.cases_created)
    except LockAcquisitionError:
        logger.info("scheduler skipped detection; lock already held")

    case_ids = await _active_case_ids()
    for case_id in case_ids:
        try:
            async with acquire_lock(f"recovery-case:{case_id}", ttl_seconds=_CYCLE_LOCK_TTL_SECONDS):
                async with async_session_factory() as session:
                    await run_recovery_cycle(session, case_id)
            summary["cycles_run"] += 1
        except LockAcquisitionError:
            summary["cycles_skipped_locked"] += 1
            logger.info("scheduler skipped case; lock already held", extra={"case_id": str(case_id)})
        except Exception:
            summary["cycles_failed"] += 1
            logger.exception("scheduler cycle failed", extra={"case_id": str(case_id)})

    logger.info(
        "scheduler tick finished marked=%s created=%s ran=%s skipped=%s failed=%s",
        summary["invoices_marked_overdue"],
        summary["cases_created"],
        summary["cycles_run"],
        summary["cycles_skipped_locked"],
        summary["cycles_failed"],
        extra=summary,
    )
    return summary


async def _active_case_ids() -> list[UUID]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(RecoveryCase.id)
            .where(RecoveryCase.status.in_(_ACTIVE_STATUSES))
            .order_by(RecoveryCase.opened_at)
        )
        return list(result.scalars().all())


async def _loop() -> None:
    delay = max(0, settings.scheduler_initial_delay_seconds)
    if delay:
        logger.info("scheduler waiting %ss before first tick", delay)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=delay)
            return
        except TimeoutError:
            pass

    interval = max(5, settings.scheduler_interval_seconds)
    while not _stop.is_set():
        try:
            await run_scheduler_tick()
        except Exception:
            logger.exception("scheduler tick crashed")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
            return
        except TimeoutError:
            continue


def start_scheduler() -> None:
    global _task
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled (SCHEDULER_ENABLED is not true)")
        return
    if _task is not None and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop(), name="recovery-scheduler")
    logger.info(
        "scheduler started interval_seconds=%s",
        settings.scheduler_interval_seconds,
        extra={"interval_seconds": settings.scheduler_interval_seconds},
    )


async def stop_scheduler() -> None:
    global _task
    _stop.set()
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    logger.info("scheduler stopped")
