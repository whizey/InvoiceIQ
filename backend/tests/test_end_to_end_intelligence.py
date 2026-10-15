"""Real trained inference and DB/tool execution; customer delivery is simulated."""

import json
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.core.config import settings
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from app.core.db import async_session_factory, engine
from app.core.time import utc_today
from app.main import app
from app.models import AgentDecision, AuditLog, Company, Invoice, OutboxEvent
from app.models.enums import CompanySegment


@pytest.mark.asyncio
@pytest.mark.parametrize("review_decision", ["approve", "reject"])
async def test_ingestion_prediction_investigation_review_and_audit(monkeypatch, review_decision):
    await engine.dispose()
    monkeypatch.setenv("PAYMENT_RISK_MODEL", "b2b_v2")
    if os.getenv("TEST_REDIS_URL"):
        monkeypatch.setattr(settings,"redis_url",os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("REVIEWER_API_KEYS", json.dumps({"integration-reviewer": "test-only-review-token"}))
    async with async_session_factory() as session:
        company = Company(name="Isolated integration fixture", industry="Testing", segment=CompanySegment.MID_MARKET)
        session.add(company)
        await session.commit()
        company_id = company.id
    due = utc_today() - timedelta(days=5)
    posting = due - timedelta(days=30)
    payload = {"company_id": str(company_id), "invoice_number": f"TEST-E2E-{uuid4().hex}",
               "amount_total": "1200000.00", "currency": "INR", "issue_date": posting.isoformat(),
               "posting_date": posting.isoformat(), "due_date": due.isoformat(), "payment_term": 30,
               "payment_method_description": "Test bank transfer"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            invalid = await client.post("/invoices", json={**payload, "payment_term": 5})
            assert invalid.status_code == 400
            start = perf_counter()
            ingested = await client.post("/invoices", json=payload)
            ingestion_ms = (perf_counter() - start) * 1000
            assert ingested.status_code == 201, ingested.text
            invoice = ingested.json()
            assert invoice["payment_risk_model_version"].startswith("b2b_v2:")
            assert 0 <= float(invoice["late_payment_probability"]) <= 1
            # Persisted invoice summaries deliberately expose the top five drivers.
            assert len(invoice["payment_risk_drivers"]) == 5
            assert (await client.post("/invoices", json=payload)).status_code == 409
            assert invoice["payment_risk_scored_at"]
            assert invoice["payment_risk_model"] == "b2b_v2"
            detected = await client.post("/recovery-cases/detect-overdue")
            assert detected.status_code == 200
            async with async_session_factory() as session:
                from app.models import RecoveryCase
                stored = await session.get(Invoice, UUID(invoice["id"]))
                assert stored.payment_risk_model_version == invoice["payment_risk_model_version"]
                case = (await session.execute(select(RecoveryCase).where(RecoveryCase.invoice_id == stored.id))).scalar_one()
                case_id = str(case.id)
            start = perf_counter()
            investigated = await client.post(f"/recovery-cases/{case_id}/run", headers={"Idempotency-Key":"acceptance-investigation"})
            investigation_ms = (perf_counter() - start) * 1000
            assert investigated.status_code == 200, investigated.text
            detail = investigated.json()
            pending = [a for a in detail["actions"] if a["status"] == "PENDING_APPROVAL"]
            assert len(pending) == 1
            assert pending[0]["executed_at"] is None
            assert detail["communication_logs"] == []
            action_id = pending[0]["id"]
            again = await client.post(f"/recovery-cases/{case_id}/run")
            assert again.status_code == 200
            assert len(again.json()["actions"]) == len(detail["actions"])
            assert (await client.post("/recovery-cases/detect-overdue")).json()["cases_created"] == 0
            reject_path = f"/recovery-cases/actions/{action_id}/reject"
            assert (await client.post(reject_path, json={})).status_code == 401
            assert (await client.post(reject_path, json={}, headers={"Authorization": "Bearer unauthorized"})).status_code == 403
            assert (await client.post(f"/recovery-cases/actions/{action_id}/approve", json={}, headers={"Authorization": "Bearer unauthorized"})).status_code == 403
            path = f"/recovery-cases/actions/{action_id}/{review_decision}"
            assert (await client.post(path, json={})).status_code == 401
            headers = {"Authorization": "Bearer test-only-review-token"}
            assert (await client.post(path, json={}, headers=headers)).status_code == 200
            assert (await client.post(path, json={}, headers=headers)).status_code == 409
            replay = await client.post(f"/recovery-cases/{case_id}/run", headers={"Idempotency-Key":"acceptance-investigation"})
            assert replay.status_code == 200
            assert len(replay.json()["actions"]) == len(detail["actions"])
            final = (await client.get(f"/recovery-cases/{case_id}")).json()
            final_status = "EXECUTED" if review_decision == "approve" else "POLICY_REJECTED"
            review_event = "REVIEW_APPROVED" if review_decision == "approve" else "REVIEW_REJECTED"
            assert final["actions"][0]["status"] == final_status
            if review_decision == "approve":
                assert final["actions"][0]["result"]["simulated"] is True
                assert final["communication_logs"][0]["status"] == "SIMULATED"
            else:
                assert final["actions"][0]["executed_at"] is None
                assert final["communication_logs"] == []
            assert any(row["event_type"] == review_event for row in final["audit_logs"])
            async with async_session_factory() as session:
                decisions = (await session.execute(select(AgentDecision).where(AgentDecision.recovery_case_id == case.id))).scalars().all()
                diagnosis = next(row for row in decisions if "sources" in row.output)
                assert diagnosis.output["sources"]
                invoice_source = next(item for item in diagnosis.output["sources"]
                                      if item["source_id"] == "invoice:" + invoice["id"])
                assert invoice_source["source_type"] == "invoice"
                assert invoice_source["source_invoice_id"] == invoice["id"]
                assert "source_sha256" not in invoice_source and "start_line" not in invoice_source
                assert diagnosis.output["verified_invoice_context"] == {
                    key: diagnosis.input_context["invoice"][key]
                    for key in ("invoice_id", "amount_total", "amount_paid", "due_date", "currency")}
                recommendation = next(row for row in decisions if row.output.get("investigation_id") and "reasons" in row.output)
                assert recommendation.output["investigation_id"] == diagnosis.output["investigation_id"]
                assert recommendation.output["execution_mode"] == "deterministic_fallback"
                assert recommendation.output["uncertainty"] and recommendation.output["reasons"]
                assert set(recommendation.output["evidence_references"]) <= {item["source_id"] for item in diagnosis.output["sources"]}
                assert recommendation.output["evidence_references"]
                from app.ml.payment_features import PaymentRiskFeatures
                from app.ml.payment_risk_model import predict_payment_risk
                from app.ml.champion_serving import verified_contract, _load_verified_model
                import math
                stored = await session.get(Invoice, UUID(invoice["id"]))
                features = PaymentRiskFeatures(**{key: float("nan") if value is None else value for key, value in stored.payment_risk_features.items()})
                prediction = predict_payment_risk(features, "b2b_v2")
                assert abs(prediction.late_payment_probability - float(stored.late_payment_probability)) <= 0.000001
                contract = verified_contract()
                assert list(_load_verified_model(contract["version"]).feature_names_in_) == contract["metadata"]["features"]
                assert {item.feature for item in prediction.contributions} == set(contract["metadata"]["features"])
                assert all(math.isfinite(item.shap_value) and not isinstance(item.value, (bool, str)) for item in prediction.contributions)
                query = diagnosis.input_context["customer"]["read_only_investigation"]
                assert query["mode"] == "constrained_deterministic_nl2sql"
                assert query["rows"]
                events = (await session.execute(select(OutboxEvent).where(OutboxEvent.key == case_id))).scalars().all()
                assert {e.topic for e in events} >= {"review.requested", "review.resolved"}
                if review_decision == "approve":
                    assert "recovery.action_completed" in {e.topic for e in events}
                else:
                    assert "recovery.action_completed" not in {e.topic for e in events}
            monkeypatch.setenv("ANALYTICS_API_KEYS", json.dumps({"acceptance": {"token": "test-only-analytics", "company_ids": [str(company_id)]}}))
            dates = {"start_date": posting.isoformat(), "end_date": datetime.now(ZoneInfo(settings.business_timezone)).date().isoformat()}
            analytics = await client.get("/dashboard/analytics", params=dates, headers={"Authorization": "Bearer test-only-analytics"})
            assert analytics.status_code == 200, analytics.text
            snapshot = analytics.json()
            assert snapshot["summary"]["invoice_count"] == 1
            assert snapshot["summary"]["predictions"] == 1
            assert snapshot["inference_requests"] == 1 and snapshot["inference_errors"] == 0
            assert snapshot["inference_latency_ms"] >= 0
            assert snapshot["historical_as_of_available"] is False
            assert snapshot["historical"]["available"] is False
            assert snapshot["historical"]["outstanding_amount"] is None
            assert float(snapshot["summary"]["outstanding_amount"]) == 1200000
            ml = (await client.get("/dashboard/analytics", params={**dates,"view":"ml"}, headers={"Authorization":"Bearer test-only-analytics"})).json()
            assert ml["inference_total"] == 1
            assert ml["inference_records"][0]["model_version"] == invoice["payment_risk_model_version"]
            assert ml["inference_records"][0]["source"] == "invoice_scoring"
            assert snapshot["summary"]["approved_approvals"] == (1 if review_decision == "approve" else 0)
            assert snapshot["summary"]["rejected_approvals"] == (1 if review_decision == "reject" else 0)
            assert snapshot["summary"]["pending_approvals"] == 0
            assert any(row["id"] == action_id and row["status"] == final_status for row in snapshot["activities"])
            assert any(row["status"] == review_event for row in snapshot["activities"])
            if review_decision == "approve":
                payment_path = f"/invoices/{invoice['id']}/simulate-payment"
                payment_headers = {"Idempotency-Key": "acceptance-payment", "Authorization": "Bearer test-only-analytics"}
                paid = await client.post(payment_path, headers=payment_headers)
                assert paid.status_code == 200, paid.text
                assert (await client.post(payment_path, headers=payment_headers)).status_code == 200
                closed = await client.post(f"/recovery-cases/{case_id}/run", headers={"Idempotency-Key":"acceptance-close", "Authorization":"Bearer test-only-analytics"})
                assert closed.status_code == 200, closed.text
                assert closed.json()["status"] in ("CLOSED", "RECOVERED")
                collected = (await client.get("/dashboard/analytics", params=dates, headers={"Authorization":"Bearer test-only-analytics"})).json()
                assert float(collected["summary"]["recovered_amount"]) == 1200000
                assert float(collected["summary"]["outstanding_amount"]) == 0
                assert collected["summary"]["active_cases"] == 0
                assert collected["prediction_outcomes"]["resolved_with_payment_events"] == 1
                assert collected["prediction_outcomes"]["late"] == 1
                from app.models import Payment
                async with async_session_factory() as session:
                    assert await session.scalar(select(func.count()).select_from(Payment).where(Payment.invoice_id == UUID(invoice["id"]))) == 1
                    events = (await session.execute(select(OutboxEvent).where(OutboxEvent.key.in_([case_id, invoice["id"]])))).scalars().all()
                assert "payment.received" in {event.topic for event in events}
            from app.events.outbox import dispatch_batch
            from app.events.log_publisher import LogEventPublisher
            event_ids = {event.id for event in events}
            address = os.getenv("TEST_KAFKA_BOOTSTRAP_SERVERS")
            consumer = None
            if address:
                from aiokafka import AIOKafkaConsumer
                from app.events.kafka_publisher import KafkaEventPublisher
                publisher = KafkaEventPublisher(address)
                consumer = AIOKafkaConsumer(*sorted({event.topic for event in events}),bootstrap_servers=address,
                    group_id="workflow-acceptance-"+uuid4().hex,auto_offset_reset="earliest",enable_auto_commit=False)
            else:
                publisher = LogEventPublisher()
            try:
                if consumer:
                    await consumer.start()
                delivered = await dispatch_batch(async_session_factory, publisher, event_ids=event_ids, limit=100)
                assert delivered["delivered"] == len(event_ids)
                if consumer:
                    async def receive_all():
                        received=set()
                        async for message in consumer:
                            identifier=json.loads(message.value).get("event_id")
                            if identifier in {str(event_id) for event_id in event_ids}:
                                received.add(identifier)
                            if len(received)==len(event_ids):
                                return received
                    assert len(await asyncio.wait_for(receive_all(),timeout=30))==len(event_ids)
                repeated = await dispatch_batch(async_session_factory, publisher, event_ids=event_ids, limit=100)
                assert repeated["delivered"] == 0
                async with async_session_factory() as session:
                    rows = (await session.execute(select(OutboxEvent).where(OutboxEvent.id.in_(event_ids)))).scalars().all()
                    assert all(row.published_at and row.delivery_mode == publisher.delivery_mode for row in rows)
            finally:
                if consumer:
                    await consumer.stop()
                await publisher.close()
            # Aggregate timings only; no individual predictions printed.
            print(f"E2E ingestion including real inference: {ingestion_ms:.1f} ms; investigation: {investigation_ms:.1f} ms")
    finally:
        async with async_session_factory() as session:
            # Owned fixture only; never reseed the development database.
            await session.execute(delete(OutboxEvent).where(OutboxEvent.key.in_([locals().get("case_id", ""), locals().get("invoice", {}).get("id", "")])))
            await session.execute(delete(AuditLog).where(AuditLog.entity_id == UUID(locals().get("invoice", {}).get("id", str(uuid4())))))
            from app.models import InferenceRecord
            await session.execute(delete(InferenceRecord).where(InferenceRecord.company_id == company_id))
            await session.execute(delete(Company).where(Company.id == company_id))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_simulated_delivery_rolls_back_then_retry_and_policy_block(monkeypatch):
    """Mock only the delivery side effect; real graph, policy and transactions."""
    from app.agents import graph
    from app.models import RecoveryCase, RecoveryAction, AuditLog
    await engine.dispose()
    monkeypatch.setenv("PAYMENT_RISK_MODEL", "b2b_v2")
    async with async_session_factory() as db:
        company = Company(name="Synthetic retry acceptance", industry="Testing", segment=CompanySegment.SMB)
        db.add(company)
        await db.commit()
        company_id = company.id
    case_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
            posting = utc_today() - timedelta(days=35)
            invoice = await client.post("/invoices", json={"company_id":str(company_id),"invoice_number":f"RETRY-{uuid4().hex}",
                "amount_total":"20000.00","currency":"INR","issue_date":posting.isoformat(),"posting_date":posting.isoformat(),
                "due_date":(posting+timedelta(days=30)).isoformat(),"payment_term":30,"payment_method_description":"Synthetic bank transfer"})
            assert invoice.status_code == 201
            assert invoice.json()["payment_risk_model_version"].startswith("b2b_v2:")
            await client.post("/recovery-cases/detect-overdue")
            async with async_session_factory() as db:
                case_id = str(await db.scalar(select(RecoveryCase.id).where(RecoveryCase.invoice_id==UUID(invoice.json()["id"])) ))
            original = graph.execute_mock_action
            async def failed_delivery(*args, **kwargs):
                raise ConnectionError("Synthetic outbound delivery failure")
            monkeypatch.setattr(graph,"execute_mock_action",failed_delivery)
            path=f"/recovery-cases/{case_id}/run"
            header={"Idempotency-Key":"retry-after-failure"}
            failed = await client.post(path, headers=header)
            assert failed.status_code == 200, failed.text

            failed_detail = failed.json()
            assert len(failed_detail["actions"]) == 1
            failed_action = failed_detail["actions"][0]

            assert failed_action["status"] == "FAILED"
            assert failed_action["result"]["success"] is False
            assert failed_action["result"]["outcome_verified"] is False
            assert failed_detail["communication_logs"] == []

            async with async_session_factory() as db:
                actions = (await db.execute(
                    select(RecoveryAction).where(
                        RecoveryAction.recovery_case_id == UUID(case_id)
                    )
                )).scalars().all()

                assert len(actions) == 1
                assert actions[0].status.value == "FAILED"
                assert actions[0].executed_at is None

                failures = await db.scalar(
                    select(func.count()).select_from(AuditLog).where(
                        AuditLog.recovery_case_id == UUID(case_id),
                        AuditLog.event_type == "ACTION_FAILED",
                    )
                )
                assert failures == 1

                topics_recorded = (await db.execute(
                    select(OutboxEvent.topic).where(
                        OutboxEvent.key == case_id
                    )
                )).scalars().all()

                assert "recovery.case_created" in topics_recorded
                assert "recovery.action_completed" not in topics_recorded

            same_request = await client.post(path, headers=header)
            assert same_request.status_code == 200
            assert len(same_request.json()["actions"]) == 1

            monkeypatch.setattr(graph, "execute_mock_action", original)

            retry_header = {
                "Idempotency-Key": "retry-after-failure-success"
            }
            retried = await client.post(path, headers=retry_header)

            assert retried.status_code == 200, retried.text
            retry_actions = retried.json()["actions"]
            assert len(retry_actions) == 2

            assert any(
                action["status"] == "EXECUTED"
                and action["result"].get("simulated") is True
                for action in retry_actions
            )

            repeated_retry = await client.post(
                path, headers=retry_header
            )
            assert repeated_retry.status_code == 200
            assert len(repeated_retry.json()["actions"]) == 2

            blocked=await client.post(path,headers={"Idempotency-Key":"second-distinct-investigation"})
            assert blocked.status_code==200,blocked.text
            detail=blocked.json()
            last=detail["actions"][-1]
            assert last["recommended_action_type"]=="SEND_PAYMENT_LINK"
            assert last["action_type"]=="WAIT"
            assert last["policy_decisions"][0]["decision"]=="REJECTED"
            assert last["policy_decisions"][0]["rule"]=="cooldown_not_elapsed"
            assert len(detail["communication_logs"])==1
    finally:
        async with async_session_factory() as db:
            if case_id:
                await db.execute(delete(OutboxEvent).where(OutboxEvent.key==case_id))
            await db.execute(delete(Company).where(Company.id==company_id))
            await db.commit()
        await engine.dispose()
