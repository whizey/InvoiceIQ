"""Prepare the historical PostgreSQL fixture in a fresh disposable replay DB."""

import asyncio
from datetime import date
import json
import os
import secrets
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from scripts.test_database_safety import verify


async def main():
    source = os.environ["PHASE1_TEST_DATABASE_URL"]
    replay = os.environ["PHASE1_REPLAY_DATABASE_URL"]
    if source == replay:
        raise RuntimeError("Corpus and replay databases must be separate")
    os.environ["PHASE1_SOURCE_DATABASE_URL"] = source
    os.environ["DATABASE_URL"] = replay

    # Application modules create an engine at import time.
    from app.core.db import async_session_factory, engine
    from app.core.time import historical_as_of
    from app.main import app
    from app.models import AuditLog, RecoveryCase
    from app.models.enums import AuditActor
    from app.seed.historical_replay import reconstruct
    from app.services.risk_engine import run_detection

    cutoff = date(2015, 2, 3)
    async with async_session_factory() as session:
        existing = (await session.scalars(select(RecoveryCase.id).limit(1))).first()
    if existing is not None:
        raise RuntimeError("Replay fixture already populated; refusing to overwrite it")
    identity = await reconstruct(cutoff)
    token = secrets.token_urlsafe(32)
    os.environ["REVIEWER_API_KEYS"] = json.dumps({"isolated-phase1-reviewer": token})
    with historical_as_of(cutoff):
        async with async_session_factory() as session:
            detection = await run_detection(session, today=cutoff,
                invoice_ids={UUID(value) for value in identity["selected_invoice_ids"]})
            assert len(detection.cases_created) == 3
            cases = (await session.scalars(select(RecoveryCase).where(
                RecoveryCase.invoice_id.in_(identity["selected_invoice_ids"])))).all()
            by_invoice = {str(case.invoice_id): str(case.id) for case in cases}
            for case in cases:
                session.add(AuditLog(recovery_case_id=case.id,
                    entity_type="recovery_case", entity_id=case.id,
                    event_type="HISTORICAL_SIMULATION_CONTEXT", actor=AuditActor.SYSTEM,
                    description="Simulated workflow on real historical invoices; no customer communication sent.",
                    metadata_={"historical_replay_as_of": cutoff.isoformat(),
                        "source_dataset_as_of": identity["source_dataset_as_of"],
                        "prediction_context": "retrospective posting-time inference, not historical model deployment"}))
            await session.commit()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated-test") as client:
            details = []
            for invoice_id in identity["selected_invoice_ids"]:
                response = await client.post(f"/recovery-cases/{by_invoice[invoice_id]}/run",
                    headers={"Idempotency-Key": "historical-first-cycle"})
                assert response.status_code == 200, response.text
                details.append(response.json())
            held = [action for action in details[0]["actions"] if action["status"] == "PENDING_APPROVAL"]
            assert len(held) == 1
            approved = await client.post(f"/recovery-cases/actions/{held[0]['id']}/approve",
                json={}, headers={"Authorization": f"Bearer {token}"})
            assert approved.status_code == 200, approved.text
            ordinary = by_invoice[identity["selected_invoice_ids"][1]]
            second = await client.post(f"/recovery-cases/{ordinary}/run",
                headers={"Idempotency-Key": "historical-second-cycle"})
            assert second.status_code == 200, second.text
    await engine.dispose()
    print("Prepared historical replay fixture in verified disposable database")


if __name__ == "__main__":
    verify(os.environ["PHASE1_TEST_DATABASE_URL"])
    verify(os.environ["PHASE1_REPLAY_DATABASE_URL"])
    asyncio.run(main())
