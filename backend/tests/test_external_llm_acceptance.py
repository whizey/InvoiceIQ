"""Explicit opt-in, actual provider through the real graph; synthetic context only."""

import os
from datetime import timedelta
from uuid import uuid4, UUID
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from app.core.config import Settings, settings
from app.core.db import async_session_factory, engine
from app.core.time import utc_today
from app.main import app
from app.models import Company, RecoveryCase, AgentDecision, InferenceRecord, OutboxEvent
from app.models.enums import CompanySegment, AgentDecisionStage


@pytest.mark.asyncio
async def test_actual_provider_investigation_never_claims_fallback(monkeypatch):
    if os.getenv("TEST_EXTERNAL_LLM") != "1":
        pytest.skip("Opt-in real LLM: TEST_EXTERNAL_LLM=1; configure a private provider key")
    configured = Settings()
    provider = os.getenv("TEST_LLM_PROVIDER", "")
    keys = {"gemini": "google_api_key", "openai": "openai_api_key", "groq": "groq_api_key"}
    for name in keys.values():
        monkeypatch.setattr(
            settings, name, getattr(configured, name) if not provider or keys.get(provider) == name else None
        )
    monkeypatch.setattr(settings, "groq_model", configured.groq_model)
    monkeypatch.setattr(settings, "llm_model", configured.llm_model)
    from app.agents.llm_client import configured_llm, FALLBACK_MODEL_NAME

    selected, _ = configured_llm()
    assert (
        selected != "fallback"
    ), "Missing prerequisite: configure a private external LLM key/model; no fallback satisfies this test"
    await engine.dispose()
    async with async_session_factory() as db:
        company = Company(
            name="Synthetic external-provider acceptance",
            industry="Testing",
            segment=CompanySegment.MID_MARKET,
        )
        db.add(company)
        await db.commit()
        company_id = company.id
    invoice_id = None
    try:
        day = utc_today() - timedelta(days=5)
        posting = day - timedelta(days=30)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/invoices",
                json={
                    "company_id": str(company_id),
                    "invoice_number": "LLM-TEST-" + uuid4().hex,
                    "amount_total": "1200000",
                    "currency": "INR",
                    "issue_date": posting.isoformat(),
                    "posting_date": posting.isoformat(),
                    "due_date": day.isoformat(),
                    "payment_term": 30,
                    "payment_method_description": "Synthetic bank transfer",
                },
            )
            assert response.status_code == 201, response.status_code
            invoice_id = UUID(response.json()["id"])
            await client.post("/recovery-cases/detect-overdue")
            async with async_session_factory() as db:
                case = await db.scalar(select(RecoveryCase).where(RecoveryCase.invoice_id == invoice_id))
            response = await client.post(
                f"/recovery-cases/{case.id}/run", headers={"Idempotency-Key": "external-provider-test"}
            )
            assert response.status_code == 200, response.status_code
            async with async_session_factory() as db:
                decisions = (
                    await db.scalars(select(AgentDecision).where(AgentDecision.recovery_case_id == case.id))
                ).all()
            diagnosis = next(d for d in decisions if d.stage == AgentDecisionStage.DIAGNOSIS)
            recommendation = next(
                d for d in decisions if d.stage == AgentDecisionStage.INTERVENTION_RECOMMENDATION
            )
            assert (
                diagnosis.model_name != FALLBACK_MODEL_NAME
            ), "Real diagnosis failed or fell back; external execution unverified"
            assert (
                recommendation.output["execution_mode"] == "external_llm"
            ), "Real recommendation failed or fell back"
            assert diagnosis.output["investigation_id"] == recommendation.output["investigation_id"]
            assert recommendation.output["evidence_references"]
            assert all(c["status"] == "SIMULATED" for c in response.json()["communication_logs"])
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(InferenceRecord).where(InferenceRecord.company_id == company_id))
            await db.execute(
                delete(OutboxEvent).where(OutboxEvent.payload["data"]["invoice_id"].astext == str(invoice_id))
            )
            await db.execute(delete(Company).where(Company.id == company_id))
            await db.commit()
        await engine.dispose()
