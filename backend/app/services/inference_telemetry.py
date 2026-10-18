"""Persist every actual model invocation independently of financial transactions.

Cache hits are not inference requests. Error records contain safe class codes,
never exception strings or credentials. Financial rollbacks do not erase attempts.
"""
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from time import perf_counter
from collections.abc import Awaitable, Callable
from typing import TypeVar, Literal

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from app.core.config import settings
from app.core.json_values import json_value
from app.models import InferenceRecord

Result = TypeVar("Result")

# Independent, loop-safe connections: telemetry commits must not depend on a
# request's financial transaction, or on TestClient's separate event loop.
_telemetry_engine = create_async_engine(settings.database_url, poolclass=NullPool)
_telemetry_sessions = async_sessionmaker(_telemetry_engine, expire_on_commit=False)


async def dispose_telemetry_engine() -> None:
    await _telemetry_engine.dispose()


async def tracked_inference(operation: Callable[[], Awaitable[Result]], *, model_kind: Literal["b2b", "bpi"],
                            model_name: str, source: str, invoice_id=None, company_id=None, workspace_id=None,
                            currency: str | None = None, input_metadata: dict | None = None) -> Result:
    started_at = datetime.now(timezone.utc)
    record = InferenceRecord(invoice_id=invoice_id, company_id=company_id, workspace_id=workspace_id,
        model_kind=model_kind, model_name=model_name, source=source, started_at=started_at,
        currency=currency, status="RUNNING", metadata_=json_value(input_metadata or {}))
    # Persist before invocation: process termination leaves an explicit unfinished request.
    async with _telemetry_sessions() as db:
        db.add(record)
        await db.commit()
        record_id = record.id
    clock = perf_counter()
    result = None
    failure = None
    try:
        result = await operation()
    except (Exception, asyncio.CancelledError) as error:
        failure = error
    completed_at = datetime.now(timezone.utc)
    latency_ms = (perf_counter() - clock) * 1000
    metadata = json_value(asdict(result)) if is_dataclass(result) else json_value(result or {})
    async with _telemetry_sessions() as db:
        record = await db.get(InferenceRecord, record_id)
        record.model_version = getattr(result, "model_version", None) or (metadata.get("model_version") if isinstance(metadata, dict) else None)
        record.completed_at = completed_at
        record.latency_ms = latency_ms
        record.status = "CANCELLED" if isinstance(failure, asyncio.CancelledError) else "FAILED" if failure else "SUCCEEDED"
        record.error_code = type(failure).__name__ if failure else None
        record.probability = getattr(result, "late_payment_probability", None)
        record.metadata_ = {**json_value(input_metadata or {}), "output": metadata}
        await db.commit()
    if failure:
        raise failure
    return result
