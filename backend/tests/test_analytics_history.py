"""Real database triggers, as-of queries, independent telemetry and scoped history."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, func, update
from app.core.analytics_auth import AnalyticsIdentity
from app.core.db import async_session_factory, engine
from app.models import (
    Company,
    Invoice,
    RecoveryCase,
    AnalyticsHistoryState,
    InvoiceStateSnapshot,
    CaseStateSnapshot,
    InferenceRecord,
)
from app.models.enums import CompanySegment, InvoiceStatus, RecoveryCaseStatus
from app.schemas.analytics import AnalyticsRange
from app.services.analytics_history import historical_snapshot, inference_telemetry
from app.services.inference_telemetry import tracked_inference


@pytest_asyncio.fixture(autouse=True)
async def isolated_event_loop_connections():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_database_triggers_capture_changes_and_rollback_with_business_state():
    await engine.dispose()
    async with async_session_factory() as db:
        c = Company(name="Synthetic ledger test", segment=CompanySegment.SMB)
        db.add(c)
        await db.flush()
        i = Invoice(
            company_id=c.id,
            invoice_number="HISTORY-" + uuid4().hex,
            amount_total=100,
            amount_paid=0,
            currency="INR",
            issue_date=date(2026, 1, 1),
            due_date=date(2026, 2, 1),
            status=InvoiceStatus.SENT,
        )
        db.add(i)
        await db.flush()
        case = RecoveryCase(company_id=c.id, invoice_id=i.id, revenue_at_risk=100)
        db.add(case)
        await db.flush()
        baseline = (
            await db.scalars(select(InvoiceStateSnapshot).where(InvoiceStateSnapshot.invoice_id == i.id))
        ).all()
        assert len(baseline) == 1 and baseline[0].amount_paid == 0 and baseline[0].source == "recorded_change"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CaseStateSnapshot)
                .where(CaseStateSnapshot.case_id == case.id)
            )
            == 1
        )
        invoice_id, case_id = i.id, case.id
        async with db.begin_nested() as transaction:
            # Core UPDATE verifies coverage isn't limited to ORM services.
            await db.execute(
                update(Invoice)
                .where(Invoice.id == i.id)
                .values(amount_paid=25, status=InvoiceStatus.PARTIALLY_PAID)
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(InvoiceStateSnapshot)
                    .where(InvoiceStateSnapshot.invoice_id == i.id)
                )
                == 2
            )
            await transaction.rollback()
        assert (
            await db.scalar(
                select(func.count())
                .select_from(InvoiceStateSnapshot)
                .where(InvoiceStateSnapshot.invoice_id == invoice_id)
            )
            == 1
        )
        await db.execute(
            update(RecoveryCase).where(RecoveryCase.id == case_id).values(status=RecoveryCaseStatus.RECOVERED)
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CaseStateSnapshot)
                .where(CaseStateSnapshot.case_id == case_id)
            )
            == 2
        )
        # The complete test transaction is rolled back; no shared business fixture remains.
        await db.rollback()


@pytest.mark.asyncio
async def test_historical_asof_uses_prior_state_not_current_balance_and_scopes():
    await engine.dispose()
    async with async_session_factory() as db:
        c = Company(name="Synthetic historical cohort", segment=CompanySegment.SMB)
        db.add(c)
        await db.flush()
        i = Invoice(
            company_id=c.id,
            invoice_number="ASOF-" + uuid4().hex,
            amount_total=100,
            amount_paid=100,
            currency="INR",
            issue_date=date(2026, 5, 1),
            due_date=date(2026, 5, 15),
            status=InvoiceStatus.PAID,
        )
        db.add(i)
        await db.flush()
        case = RecoveryCase(
            company_id=c.id,
            invoice_id=i.id,
            revenue_at_risk=0,
            status=RecoveryCaseStatus.RECOVERED,
            opened_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        )
        db.add(case)
        await db.flush()
        # Synthetic event-time fixture, inside an uncommitted transaction only.
        coverage_update = await db.execute(
            update(AnalyticsHistoryState)
            .where(AnalyticsHistoryState.name == "financial_snapshots")
            .values(started_at=datetime(2026, 5, 1, tzinfo=timezone.utc))
        )
        if coverage_update.rowcount == 0:
            db.add(
                AnalyticsHistoryState(
                    name="financial_snapshots",
                    started_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                )
            )
            await db.flush()
        db.add_all(
            [
                InvoiceStateSnapshot(
                    invoice_id=i.id,
                    company_id=c.id,
                    observed_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
                    source="synthetic_fixture",
                    amount_total=100,
                    amount_paid=0,
                    currency="INR",
                    posting_date=date(2026, 5, 1),
                    due_date=date(2026, 5, 15),
                    status="SENT",
                ),
                InvoiceStateSnapshot(
                    invoice_id=i.id,
                    company_id=c.id,
                    observed_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
                    source="synthetic_fixture",
                    amount_total=100,
                    amount_paid=100,
                    currency="INR",
                    posting_date=date(2026, 5, 1),
                    due_date=date(2026, 5, 15),
                    status="PAID",
                ),
                CaseStateSnapshot(
                    case_id=case.id,
                    invoice_id=i.id,
                    company_id=c.id,
                    observed_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
                    opened_at=case.opened_at,
                    status="OPEN",
                    source="synthetic_fixture",
                ),
                CaseStateSnapshot(
                    case_id=case.id,
                    invoice_id=i.id,
                    company_id=c.id,
                    observed_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
                    opened_at=case.opened_at,
                    status="RECOVERED",
                    source="synthetic_fixture",
                ),
            ]
        )
        await db.flush()
        identity = AnalyticsIdentity("synthetic-history", (c.id,))
        may = await historical_snapshot(
            db,
            AnalyticsRange(start_date=date(2026, 5, 1), end_date=date(2026, 5, 31)),
            identity,
            "Asia/Kolkata",
        )
        assert (
            may.available
            and may.outstanding_amount == 100
            and may.overdue_amount == 100
            and may.active_cases == 1
        )
        june = await historical_snapshot(
            db,
            AnalyticsRange(start_date=date(2026, 5, 1), end_date=date(2026, 6, 30)),
            identity,
            "Asia/Kolkata",
        )
        assert june.outstanding_amount == 0 and june.active_cases == 0
        outside = await historical_snapshot(
            db,
            AnalyticsRange(start_date=date(2026, 5, 1), end_date=date(2026, 5, 31)),
            AnalyticsIdentity("outside", ()),
            "Asia/Kolkata",
        )
        assert outside.invoice_count == 0 and outside.outstanding_amount == 0
        before = await historical_snapshot(
            db,
            AnalyticsRange(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)),
            identity,
            "Asia/Kolkata",
        )
        assert not before.available and before.outstanding_amount is None
        await db.rollback()


@pytest.mark.asyncio
async def test_inference_success_failure_cancellation_and_history_survive_rollback():
    await engine.dispose()
    workspace = "telemetry-test-" + uuid4().hex

    async def good():
        return {
            "model_version": "test-contract-only",
            "horizon_probabilities": {"clearance_probability_7d": 0.2},
        }

    async def bad():
        raise RuntimeError("PRIVATE_DIAGNOSTIC_MUST_NOT_BE_STORED")

    async def cancelled():
        raise asyncio.CancelledError()

    try:
        result = await tracked_inference(
            good,
            model_kind="bpi",
            model_name="component-test",
            source="component_test",
            workspace_id=workspace,
        )
        assert result["model_version"] == "test-contract-only"
        with pytest.raises(RuntimeError):
            await tracked_inference(
                bad,
                model_kind="bpi",
                model_name="component-test",
                source="component_test",
                workspace_id=workspace,
            )
        with pytest.raises(asyncio.CancelledError):
            await tracked_inference(
                cancelled,
                model_kind="bpi",
                model_name="component-test",
                source="component_test",
                workspace_id=workspace,
            )
        async with async_session_factory() as db:
            records = (
                await db.scalars(select(InferenceRecord).where(InferenceRecord.workspace_id == workspace))
            ).all()
            assert {r.status for r in records} == {"SUCCEEDED", "FAILED", "CANCELLED"}
            assert all(r.latency_ms >= 0 and r.completed_at >= r.started_at for r in records)
            assert all(
                "PRIVATE" not in str(r.metadata_) and "PRIVATE" not in (r.error_code or "") for r in records
            )
            await db.rollback()
        today = datetime.now(timezone.utc).date()
        async with async_session_factory() as db:
            telemetry = await inference_telemetry(
                db,
                AnalyticsRange(start_date=today - timedelta(days=1), end_date=today + timedelta(days=1)),
                AnalyticsIdentity(workspace, ()),
                "UTC",
            )
            assert (
                telemetry["bpi"].requests == 3
                and telemetry["bpi"].succeeded == 1
                and telemetry["bpi"].errors == 2
            )
            assert telemetry["bpi"].average_latency_ms >= 0 and telemetry["bpi"].p95_latency_ms >= 0
            assert telemetry["b2b"].requests == 0
            outside = await inference_telemetry(
                db,
                AnalyticsRange(start_date=today, end_date=today),
                AnalyticsIdentity("different", ()),
                "UTC",
            )
            assert outside["bpi"].requests == 0
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(InferenceRecord).where(InferenceRecord.workspace_id == workspace))
            await db.commit()


@pytest.mark.asyncio
async def test_trained_prediction_history_keeps_superseded_scores_and_cache_hits_are_not_requests(
    monkeypatch,
):
    from app.services.payment_risk_scoring import ensure_payment_risk_score
    from app.services.date_analytics import get_date_analytics

    monkeypatch.setenv("PAYMENT_RISK_MODEL", "b2b_v2")
    company_id = None
    try:
        async with async_session_factory() as db:
            c = Company(name="Synthetic trained inference history", segment=CompanySegment.SMB)
            db.add(c)
            await db.flush()
            company_id = c.id
            day = datetime.now(timezone.utc).date()
            i = Invoice(
                company_id=c.id,
                invoice_number="REAL-HISTORY-" + uuid4().hex,
                amount_total=100,
                amount_paid=0,
                currency="INR",
                issue_date=day,
                posting_date=day,
                due_date=day + timedelta(days=30),
                payment_term=30,
                payment_method_description="Synthetic bank transfer",
                status=InvoiceStatus.SENT,
            )
            db.add(i)
            await db.flush()
            assert await ensure_payment_risk_score(db, i)
            assert await ensure_payment_risk_score(db, i)  # Actual cache hit, no second model invocation.
            # Force the existing version-mismatch rescore path on this owned synthetic record.
            i.payment_risk_model_version = "synthetic-prior-version"
            assert await ensure_payment_risk_score(db, i)
            await db.commit()
            records = (
                await db.scalars(select(InferenceRecord).where(InferenceRecord.invoice_id == i.id))
            ).all()
            assert len(records) == 2
            assert all(r.status == "SUCCEEDED" and r.model_version.startswith("b2b_v2:") for r in records)
            assert all(len(r.metadata_["output"]["contributions"]) == 14 for r in records)
            from app.ml.payment_features import FEATURES

            assert all(r.metadata_["input_feature_order"] == FEATURES for r in records)
            assert all(set(r.metadata_["input_features"]) == set(FEATURES) for r in records)
            result = await get_date_analytics(
                db,
                AnalyticsRange(
                    start_date=day - timedelta(days=1), end_date=day + timedelta(days=1), view="ml"
                ),
                AnalyticsIdentity("trained-history", (company_id,)),
            )
            assert (
                result.summary.predictions == 2
                and result.inference_requests == 2
                and result.inference_total == 2
            )
            assert result.stored_predictions == 1
            assert result.model_versions == {i.payment_risk_model_version: 1}
            assert result.persisted_prediction_provenance["with_recorded_inference"] == 1
            assert (
                sum(
                    result.prediction_outcomes[k]
                    for k in ("resolved_with_payment_events", "ground_truth_unavailable")
                )
                == 2
            )
            assert (
                result.inference_telemetry["b2b"].succeeded == 2
                and result.inference_telemetry["bpi"].requests == 0
            )
            # Read-only analytics doesn't execute another model invocation.
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(InferenceRecord)
                    .where(InferenceRecord.company_id == company_id)
                )
                == 2
            )
    finally:
        if company_id:
            async with async_session_factory() as db:
                await db.execute(delete(InferenceRecord).where(InferenceRecord.company_id == company_id))
                await db.execute(delete(Company).where(Company.id == company_id))
                await db.commit()


@pytest.mark.asyncio
async def test_manual_trained_inference_requires_scope_and_history_is_not_shared(monkeypatch):
    import json
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.core.json_values import json_value
    from app.services.payment_risk_context import compute_payment_risk_features

    workspace = "manual-test-" + uuid4().hex
    other = "other-manual-test-" + uuid4().hex
    monkeypatch.setenv("PAYMENT_RISK_MODEL", "b2b_v2")
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS",
        json.dumps(
            {
                workspace: {"token": "scope-a-only", "company_ids": []},
                other: {"token": "scope-b-only", "company_ids": []},
            }
        ),
    )
    features = json_value(
        compute_payment_risk_features(
            amount=100, posting_date=date(2026, 1, 1), payment_term=30, history=[]
        ).to_row()
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (
                await client.post("/ml/payment-risk/predict", json={"features": features})
            ).status_code == 401
            assert (
                await client.post(
                    "/ml/payment-risk/predict",
                    json={"features": features},
                    headers={"Authorization": "Bearer wrong"},
                )
            ).status_code == 403
            prediction = await client.post(
                "/ml/payment-risk/predict",
                json={"features": features},
                headers={"Authorization": "Bearer scope-a-only"},
            )
            assert prediction.status_code == 200, prediction.text
            day = datetime.now(timezone.utc).date()
            params = {
                "start_date": (day - timedelta(days=1)).isoformat(),
                "end_date": (day + timedelta(days=1)).isoformat(),
                "view": "ml",
            }
            a = (
                await client.get(
                    "/dashboard/analytics", params=params, headers={"Authorization": "Bearer scope-a-only"}
                )
            ).json()
            b = (
                await client.get(
                    "/dashboard/analytics", params=params, headers={"Authorization": "Bearer scope-b-only"}
                )
            ).json()
            assert a["inference_requests"] == a["inference_total"] == a["summary"]["predictions"] == 1
            assert a["inference_records"][0]["model_version"] == prediction.json()["model_version"]
            assert a["prediction_outcomes"]["ground_truth_unavailable"] == 1
            assert a["summary"]["invoice_count"] == 0  # Feature inspector doesn't create financial records.
            assert b["inference_requests"] == b["inference_total"] == b["summary"]["predictions"] == 0
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(InferenceRecord).where(InferenceRecord.workspace_id == workspace))
            await db.commit()
