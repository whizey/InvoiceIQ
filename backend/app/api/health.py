import asyncio

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.db import check_db_connection

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/db")
async def health_db() -> dict:
    try:
        await check_db_connection()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Readiness requires DB and primary serving; optional research model may fail."""
    from app.api.ml_intelligence import list_models

    try:
        await asyncio.wait_for(check_db_connection(), timeout=2)
        catalog = await asyncio.wait_for(run_in_threadpool(list_models), timeout=10)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Required dependency unavailable") from exc
    if not catalog["models"][0]["available"]:
        raise HTTPException(status_code=503, detail="Primary payment-risk model unavailable")
    return {"status": "ready", "database": "available", "primary_model": catalog["primary_model"],
            "primary_model_version": catalog["models"][0]["version"],
            "bpi_available": catalog["models"][1]["available"]}
