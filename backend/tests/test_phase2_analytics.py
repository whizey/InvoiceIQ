"""Independent public-source reconciliation; read-only on the Phase 1 source DB."""

import hashlib
import os
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.analytics_auth import AnalyticsIdentity
from app.models import Company, Invoice, DatasetImport, RecoveryCase, RecoveryAction
from app.schemas.analytics import AnalyticsRange, resolve_period
from app.services.date_analytics import get_date_analytics


def test_calendar_presets_and_validation():
    assert resolve_period("3M", date(2016, 3, 17)) == (date(2015, 12, 18), date(2016, 3, 17))
    assert resolve_period("6M", date(2016, 3, 17))[0] == date(2015, 9, 18)
    assert resolve_period("1M", date(2016, 2, 29))[0] == date(2016, 2, 1)
    assert resolve_period("7D", date(2016, 3, 1))[0] == date(2016, 2, 24)
    assert resolve_period("YTD", date(2016, 3, 17))[0] == date(2016, 1, 1)


@pytest.mark.asyncio
async def test_real_source_windows_reconcile_without_financial_mutations():
    url = os.getenv("PHASE1_SOURCE_DATABASE_URL")
    if not url:
        pytest.skip("Requires verified isolated source PostgreSQL cohort")
    from app.seed.load_real_dataset import require_isolated_url

    require_isolated_url(url)
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = Path(__file__).resolve().parents[2] / "data/raw/b2b_primary/b2b_invoice_payment_45839.csv"
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    frame = pd.read_csv(source, dtype={"Amount": str})
    for name in ["Posting_Date", "Clearing_date", "Net_Due_Date"]:
        frame[name] = pd.to_datetime(frame[name]).dt.date
    frame["Amount"] = frame["Amount"].map(Decimal)
    try:
        async with sessions() as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            imported = await db.get(DatasetImport, "public_b2b_dataset")
            assert imported.dataset_as_of == date(2016, 3, 17)
            identity = AnalyticsIdentity("source-test", tuple((await db.scalars(select(Company.id))).all()))
            initial = (
                await db.execute(
                    text(
                        "SELECT (SELECT count(*) FROM invoices),(SELECT count(*) FROM payments),(SELECT count(*) FROM recovery_actions),(SELECT count(*) FROM audit_logs)"
                    )
                )
            ).one()
            cardinality = (
                await db.execute(
                    text(
                        "SELECT (SELECT count(DISTINCT invoice_number) FROM invoices), (SELECT count(DISTINCT invoice_id) FROM payments)"
                    )
                )
            ).one()
            assert initial[:2] == cardinality == (len(frame), len(frame))
            results = []
            for start, end, grain in [
                (date(2015, 12, 18), date(2016, 3, 17), "month"),
                (date(2015, 9, 18), date(2016, 3, 17), "month"),
                (date(2015, 2, 3), date(2015, 2, 3), "month"),
                (date(2015, 1, 1), date(2015, 12, 31), "quarter"),
                (date(2011, 1, 1), date(2016, 3, 17), "year"),
                (date(2011, 1, 1), date(2015, 2, 3), "month"),
                (date(2016, 2, 18), date(2016, 3, 17), "month"),
                (date(2015, 3, 18), date(2016, 3, 17), "month"),
                (date(2016, 1, 1), date(2016, 3, 17), "month"),
                (date(2000, 1, 1), date(2000, 1, 31), "month"),
            ]:
                from time import perf_counter

                clock = perf_counter()
                actual = await get_date_analytics(
                    db,
                    AnalyticsRange(
                        start_date=start,
                        end_date=end,
                        grain=grain,
                        view="invoices",
                        workflow_date_axis="invoice",
                    ),
                    identity,
                )
                selected = frame[(frame.Posting_Date >= start) & (frame.Posting_Date <= end)]
                previous = frame[
                    (frame.Posting_Date >= actual.previous_start_date)
                    & (frame.Posting_Date <= actual.previous_end_date)
                ]
                assert actual.previous.total_invoice_amount == sum(previous.Amount, Decimal(0))
                prior_payments = frame[
                    (frame.Clearing_date >= actual.previous_start_date)
                    & (frame.Clearing_date <= actual.previous_end_date)
                ]
                assert actual.previous.recovered_amount == sum(prior_payments.Amount, Decimal(0))
                collected = frame[(frame.Clearing_date >= start) & (frame.Clearing_date <= end)]
                unpaid = selected[(selected.Clearing_date > end) & (selected.Posting_Date <= end)]
                overdue = unpaid[unpaid.Net_Due_Date < end]
                assert actual.invoice_total == actual.summary.invoice_count == len(selected)
                assert actual.summary.total_invoice_amount == sum(selected.Amount, Decimal(0))
                assert actual.summary.recovered_amount == sum(collected.Amount, Decimal(0))
                assert actual.financial.outstanding_amount == sum(unpaid.Amount, Decimal(0))
                assert actual.financial.overdue_amount == sum(overdue.Amount, Decimal(0))
                assert actual.financial.overdue_invoice_count == len(overdue)
                resolved = selected[selected.Clearing_date <= end]
                delays = [
                    max(
                        0,
                        (
                            row.Clearing_date
                            - (
                                row.Net_Due_Date
                                + timedelta(
                                    days=2
                                    if row.Net_Due_Date.weekday() == 5
                                    else 1
                                    if row.Net_Due_Date.weekday() == 6
                                    else 0
                                )
                            )
                        ).days,
                    )
                    for row in resolved.itertuples()
                ]
                # The source has exactly one successful clearing per invoice;
                # compare every observed SQL delay, not only aggregate statistics.
                sql_delays = (
                    await db.scalars(
                        text("""
                    SELECT greatest((p.payment_date AT TIME ZONE 'UTC')::date -
                        (i.due_date + CASE extract(isodow FROM i.due_date)
                            WHEN 6 THEN 2 WHEN 7 THEN 1 ELSE 0 END), 0)
                    FROM invoices i JOIN payments p ON p.invoice_id=i.id
                    WHERE i.posting_date BETWEEN :start AND :end
                        AND i.currency='INR' AND p.currency='INR' AND p.status='SUCCESS'
                        AND (p.payment_date AT TIME ZONE 'UTC')::date <= :end
                """),
                        {"start": start, "end": end},
                    )
                ).all()
                assert Counter(sql_delays) == Counter(delays)
                assert actual.financial.resolved_invoice_count == len(resolved)
                assert actual.financial.late_invoice_count == sum(d > 0 for d in delays)
                if delays:
                    import statistics

                    assert abs(actual.financial.average_delay_days - statistics.mean(delays)) < 1e-9
                    assert actual.financial.median_delay_days == statistics.median(delays)
                assert actual.financial.shap_invoice_count == len(selected)
                assert all(
                    row["contribution_count"] == len(selected) for row in actual.financial.shap_importance
                )
                assert sum(actual.risk_distribution.values()) == len(selected)
                # Independently group persisted probabilities using exact SQL numerics.
                raw_risk = dict(
                    (
                        await db.execute(
                            text("""
                    SELECT CASE WHEN late_payment_probability < 0.3 THEN 'LOW'
                        WHEN late_payment_probability < 0.7 THEN 'MEDIUM' ELSE 'HIGH' END,
                        count(*) FROM invoices
                    WHERE posting_date BETWEEN :start AND :end AND currency='INR'
                    GROUP BY 1
                """),
                            {"start": start, "end": end},
                        )
                    ).all()
                )
                assert actual.risk_distribution == raw_risk
                assert actual.stored_predictions == len(selected) == sum(actual.model_versions.values())
                assert sum(bucket.invoice_count for bucket in actual.series) == len(selected)
                assert (
                    sum((bucket.invoice_amount for bucket in actual.series), Decimal(0))
                    == actual.summary.total_invoice_amount
                )
                assert (
                    sum((bucket.recovered_amount for bucket in actual.series), Decimal(0))
                    == actual.summary.recovered_amount
                )
                assert len({record.id for record in actual.invoices}) == len(actual.invoices) <= 25
                print(
                    "RECONCILIATION",
                    start,
                    end,
                    grain,
                    {
                        "invoices": len(selected),
                        "invoiced": str(actual.summary.total_invoice_amount),
                        "collected": str(actual.summary.recovered_amount),
                        "outstanding": str(actual.financial.outstanding_amount),
                        "overdue": str(actual.financial.overdue_amount),
                        "stored_predictions": actual.stored_predictions,
                        "risk": actual.risk_distribution,
                        "resolved": actual.financial.resolved_invoice_count,
                        "late": actual.financial.late_invoice_count,
                        "late_rate": actual.financial.actual_late_rate,
                        "average_delay_days": str(actual.financial.average_delay_days),
                        "median_delay_days": actual.financial.median_delay_days,
                        "delay_histogram": dict(sorted(Counter(delays).items())),
                        "milliseconds": round((perf_counter() - clock) * 1000, 1),
                    },
                )
                results.append(actual)
            assert results[0].summary.invoice_count != results[1].summary.invoice_count
            assert (
                results[2].financial.outstanding_amount > 0
            )  # Future clearing must never zero this historical balance.
            assert results[4].summary.total_invoice_amount == Decimal(816262430)
            from app.models.enums import InvoiceStatus

            historical = await get_date_analytics(
                db,
                AnalyticsRange(
                    start_date=date(2011, 1, 1),
                    end_date=date(2015, 2, 3),
                    invoice_status=InvoiceStatus.OVERDUE,
                ),
                identity,
            )
            reference = frame[
                (frame.Posting_Date >= date(2011, 1, 1))
                & (frame.Posting_Date <= date(2015, 2, 3))
                & (frame.Clearing_date > date(2015, 2, 3))
                & (frame.Net_Due_Date < date(2015, 2, 3))
            ]
            assert historical.invoice_total == len(reference) > 0
            assert all(row["status"] == "OVERDUE" for row in historical.historical_invoice_balances.values())
            print("HISTORICAL_STATUS_FILTER", {"overdue": historical.invoice_total, "cutoff": "2015-02-03"})
            plan = (
                (
                    await db.execute(
                        text(
                            "EXPLAIN (ANALYZE, BUFFERS) SELECT count(*),sum(amount_total) FROM invoices WHERE coalesce(posting_date,issue_date) BETWEEN '2015-12-18' AND '2016-03-17' AND currency='INR'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            print("QUERY_PLAN\n" + "\n".join(plan))
            assert (
                results[-1].invoices == []
                and results[-1].series == []
                and results[-1].summary.invoice_count == 0
            )
            # Stable SQL pagination/sorting and chart drilldown population.
            interval = AnalyticsRange(
                start_date=date(2015, 9, 18), end_date=date(2016, 3, 17), sort="amount_desc", page_size=10
            )
            first = await get_date_analytics(db, interval, identity)
            second = await get_date_analytics(db, interval.model_copy(update={"page": 2}), identity)
            assert {i.id for i in first.invoices}.isdisjoint({i.id for i in second.invoices})
            assert [i.amount_total for i in first.invoices] == sorted(
                [i.amount_total for i in first.invoices], reverse=True
            )
            counts = (
                await db.execute(
                    text(
                        "SELECT (SELECT count(*) FROM invoices),(SELECT count(*) FROM payments),(SELECT count(*) FROM recovery_actions),(SELECT count(*) FROM audit_logs)"
                    )
                )
            ).one()
            assert counts == initial
            assert imported.source_sha256 == before_hash
    finally:
        await engine.dispose()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash


@pytest.mark.asyncio
async def test_workflow_invoice_axis_reconciles_pending_queue_and_preserves_simulation(monkeypatch):
    """Isolated regression fixtures explicitly synthetic, never UI production data."""
    import json
    from datetime import datetime, timezone
    from uuid import uuid4
    from sqlalchemy import delete
    from app.core.db import async_session_factory, engine
    from app.models.enums import (
        CompanySegment,
        InvoiceStatus,
        RecoveryActionType,
        RecoveryActionStatus,
        ProposedBy,
    )
    from app.services.review_gate import list_pending
    from app.main import app
    from httpx import ASGITransport, AsyncClient

    await engine.dispose()
    async with async_session_factory() as db:
        company = Company(name="Synthetic Phase 2 workflow-axis test", segment=CompanySegment.UNKNOWN)
        db.add(company)
        await db.flush()
        company_id = company.id
        invoice = Invoice(
            company_id=company.id,
            invoice_number="PHASE2-" + uuid4().hex,
            amount_total=1200,
            amount_paid=0,
            currency="INR",
            issue_date=date(2015, 1, 1),
            posting_date=date(2015, 1, 1),
            due_date=date(2015, 1, 31),
            status=InvoiceStatus.SENT,
        )
        db.add(invoice)
        await db.flush()
        case = RecoveryCase(
            company_id=company.id,
            invoice_id=invoice.id,
            revenue_at_risk=1200,
            opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        db.add(case)
        await db.flush()
        db.add_all(
            [
                RecoveryAction(
                    recovery_case_id=case.id,
                    action_type=RecoveryActionType.ESCALATE,
                    recommended_action_type=RecoveryActionType.SEND_EMAIL,
                    status=RecoveryActionStatus.PENDING_APPROVAL,
                    proposed_by=ProposedBy.AI,
                    sequence_number=1,
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
                RecoveryAction(
                    recovery_case_id=case.id,
                    action_type=RecoveryActionType.SEND_EMAIL,
                    recommended_action_type=RecoveryActionType.SEND_EMAIL,
                    status=RecoveryActionStatus.EXECUTED,
                    proposed_by=ProposedBy.AI,
                    sequence_number=2,
                    result={"simulated": True},
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
            ]
        )
        await db.commit()
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS",
        json.dumps({"axis": {"token": "synthetic-axis-token", "company_ids": [str(company_id)]}}),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer synthetic-axis-token"}
            params = {"start_date": "2015-01-01", "end_date": "2015-01-31", "view": "approvals"}
            event = (await client.get("/dashboard/analytics", params=params, headers=headers)).json()
            assert event["summary"]["case_count"] == 0
            params["workflow_date_axis"] = "invoice"
            response = await client.get("/analytics", params=params, headers=headers)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["summary"]["case_count"] == data["case_total"] == 1
            assert data["summary"]["pending_approvals"] == 1
            assert data["summary"]["actions"] == 2
            assert data["summary"]["policy_overrides"] == 1
            assert len([r for r in data["activities"] if r["status"] == "PENDING_APPROVAL"]) == 1
            params["view"] = "investigations"
            history = (await client.get("/dashboard/analytics", params=params, headers=headers)).json()
            assert next(r for r in history["activities"] if r["status"] == "EXECUTED")["simulated"] is True
            params["view"] = "approvals"
            params["approval_status"] = "PENDING_APPROVAL"
            filtered = (await client.get("/dashboard/analytics", params=params, headers=headers)).json()
            assert filtered["activity_total"] == 1
            missing = (
                await client.get(
                    "/dashboard/analytics", params={**params, "case_id": str(uuid4())}, headers=headers
                )
            ).json()
            assert missing["activity_total"] == 0 and missing["summary"]["case_count"] == 0
            assert (
                await client.get("/analytics", params={**params, "grain": "week"}, headers=headers)
            ).status_code == 422
            assert (
                await client.get("/analytics", params={**params, "end_date": "2014-12-31"}, headers=headers)
            ).status_code == 422
            assert (await client.get("/analytics", params=params)).status_code == 401
        async with async_session_factory() as db:
            queue = await list_pending(
                db, company_ids=(company_id,), posting_start=date(2015, 1, 1), posting_end=date(2015, 1, 31)
            )
            assert len(queue) == data["summary"]["pending_approvals"]
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Company).where(Company.id == company_id))
            await db.commit()
        await engine.dispose()
