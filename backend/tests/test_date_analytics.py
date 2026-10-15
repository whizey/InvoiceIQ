import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from app.core.db import async_session_factory, engine
from app.main import app
from app.models import Company, Invoice, Payment, RecoveryAction, RecoveryCase
from app.models.enums import (
    CompanySegment,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
    ProposedBy,
    RecoveryActionStatus,
    RecoveryActionType,
)
from app.schemas.analytics import AnalyticsRange


def test_date_validation_leap_and_dst():
    with pytest.raises(ValidationError):
        AnalyticsRange(start_date=date(2026, 3, 2), end_date=date(2026, 3, 1))
    with pytest.raises(ValidationError):
        AnalyticsRange(start_date=date(1800, 1, 1), end_date=date(2026, 1, 1))
    for day, zone, hours in [
        (date(2024, 2, 29), "Asia/Kolkata", 24),
        (date(2026, 3, 8), "America/New_York", 23),
    ]:
        a, b = AnalyticsRange(start_date=day, end_date=day).timestamps(zone)
        assert (b - a).total_seconds() == hours * 3600


@pytest.mark.asyncio
async def test_postgres_scoped_analytics_nested_empty_and_read_only(monkeypatch):
    await engine.dispose()
    ids = []
    async with async_session_factory() as db:
        for name in ["Synthetic workspace A", "Synthetic workspace B"]:
            c = Company(name=name, industry="Testing", segment=CompanySegment.SMB)
            db.add(c)
            await db.flush()
            ids.append(c.id)
        for company, day, amount in [
            (ids[0], date(2026, 5, 1), 100),
            (ids[0], date(2026, 8, 1), 200),
            (ids[1], date(2026, 8, 1), 900),
        ]:
            i = Invoice(
                company_id=company,
                invoice_number=f"DATE-{uuid4().hex}",
                amount_total=amount,
                amount_paid=10,
                currency="INR",
                issue_date=day,
                posting_date=day,
                due_date=day,
                status=InvoiceStatus.PARTIALLY_PAID,
                late_payment_probability=Decimal("0.8"),
            )
            db.add(i)
            await db.flush()
            recovery_case = RecoveryCase(
                invoice_id=i.id,
                company_id=company,
                revenue_at_risk=amount - 10,
                opened_at=datetime.combine(day, datetime.min.time(), timezone.utc),
            )
            db.add(recovery_case)
            await db.flush()
            if company == ids[0] and day.month == 5:
                for seq, status in [
                    (1, RecoveryActionStatus.PROPOSED),
                    (2, RecoveryActionStatus.POLICY_REJECTED),
                ]:
                    db.add(
                        RecoveryAction(
                            recovery_case_id=recovery_case.id,
                            action_type=RecoveryActionType.WAIT,
                            recommended_action_type=RecoveryActionType.SEND_EMAIL,
                            status=status,
                            proposed_by=ProposedBy.AI,
                            sequence_number=seq,
                            created_at=datetime.combine(day, datetime.min.time(), timezone.utc),
                        )
                    )
            db.add(
                Payment(
                    invoice_id=i.id,
                    amount=10,
                    currency="INR",
                    payment_date=datetime.combine(day, datetime.min.time(), timezone.utc),
                    method=PaymentMethod.OTHER,
                    status=PaymentStatus.SUCCESS,
                )
            )
        await db.commit()
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS",
        json.dumps(
            {
                "a": {"token": "a-only", "company_ids": [str(ids[0])]},
                "b": {"token": "b-only", "company_ids": [str(ids[1])]},
            }
        ),
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

            async def get(start, end="2026-09-30", token="a-only", **extra):
                r = await client.get(
                    "/dashboard/analytics",
                    params={"start_date": start, "end_date": end, **extra},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert r.status_code == 200, r.text
                return r.json()

            three = await get("2026-07-01")
            six = await get("2026-04-01")
            assert three["summary"]["invoice_count"] == 1
            assert six["summary"]["invoice_count"] == 2
            assert six["summary"]["actions"] == 2
            assert six["summary"]["policy_overrides"] == 1
            assert six["action_distribution"] == {"AWAITING_POLICY": 1, "WAIT": 1}
            assert sum(six["action_distribution"].values()) == six["summary"]["actions"]
            assert Decimal(six["summary"]["total_invoice_amount"]) == 300
            assert Decimal(six["summary"]["outstanding_amount"]) == 280
            assert Decimal(six["summary"]["recovered_amount"]) == 20
            assert sum(x["invoice_count"] for x in six["series"]) == 2
            assert sum(Decimal(x["invoice_amount"]) for x in six["series"]) == 300
            assert sum(Decimal(x["recovered_amount"]) for x in six["series"]) == 20
            assert {x["id"] for x in three["invoices"]} <= {x["id"] for x in six["invoices"]}
            assert six["case_total"] == six["summary"]["case_count"] == len(six["cases"])
            assert sum(six["risk_distribution"].values()) == 2
            assert not six["historical_as_of_available"]
            assert "not historical" in six["semantics"]["outstanding_amount"]
            assert (
                Decimal((await get("2026-04-01", token="b-only"))["summary"]["total_invoice_amount"]) == 900
            )
            legacy = await client.get("/invoices", headers={"Authorization": "Bearer a-only"})
            assert legacy.status_code == 200, legacy.text
            assert {item["company"]["id"] for item in legacy.json()} == {str(ids[0])}
            outside = await client.get("/invoices", headers={"Authorization": "Bearer b-only"})
            outside_id = outside.json()[0]["id"]
            assert (
                await client.get(f"/invoices/{outside_id}", headers={"Authorization": "Bearer a-only"})
            ).status_code == 404
            assert (await client.get("/recovery-cases")).status_code == 401
            scoped_cases = await client.get("/recovery-cases", headers={"Authorization": "Bearer a-only"})
            assert len(scoped_cases.json()) == 2
            other_cases = await client.get("/recovery-cases", headers={"Authorization": "Bearer b-only"})
            other_case_id = other_cases.json()[0]["id"]
            for suffix in ["", "/risk-explanation"]:
                assert (
                    await client.get(
                        f"/recovery-cases/{other_case_id}{suffix}", headers={"Authorization": "Bearer a-only"}
                    )
                ).status_code == 404
            metrics = await client.get("/dashboard/metrics", headers={"Authorization": "Bearer a-only"})
            assert metrics.status_code == 200, metrics.text
            assert metrics.json()["total_cases"] == 2

            empty = await get("2020-01-01", "2020-02-01")
            assert empty["summary"]["invoice_count"] == 0 and empty["series"] == []
            searched = await get("2026-04-01", search="missing")
            assert searched["invoice_total"] == 0 and searched["summary"] == six["summary"]
            params = {"start_date": "2026-07-01", "end_date": "2026-09-30"}
            assert (await client.get("/dashboard/analytics", params=params)).status_code == 401
            assert (
                await client.get(
                    "/dashboard/analytics", params=params, headers={"Authorization": "Bearer wrong"}
                )
            ).status_code == 403
            params["start_date"] = "2026-10-01"
            assert (
                await client.get(
                    "/dashboard/analytics", params=params, headers={"Authorization": "Bearer a-only"}
                )
            ).status_code == 422
        async with async_session_factory() as db:
            assert (
                await db.scalar(select(func.count()).select_from(Invoice).where(Invoice.company_id.in_(ids)))
                == 3
            )
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Company).where(Company.id.in_(ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_large_cohort_query_and_half_open_payment_boundaries(monkeypatch):
    """2,000 synthetic records, scoped SQL aggregates; report measured latency."""
    from time import perf_counter
    from app.core.config import settings

    await engine.dispose()
    async with async_session_factory() as db:
        c = Company(name="Synthetic scale and boundary test", industry="Testing", segment=CompanySegment.SMB)
        db.add(c)
        await db.flush()
        company_id = c.id
        records = [
            Invoice(
                company_id=company_id,
                invoice_number=f"SCALE-{uuid4().hex}",
                amount_total=100,
                amount_paid=0,
                currency="INR",
                issue_date=date(2026, 7, 1),
                posting_date=date(2026, 7, 1),
                due_date=date(2026, 8, 1),
                status=InvoiceStatus.SENT,
            )
            for _ in range(2000)
        ]
        db.add_all(records)
        await db.flush()
        interval = AnalyticsRange(start_date=date(2026, 7, 1), end_date=date(2026, 9, 30))
        start, stop = interval.timestamps(settings.business_timezone)
        from datetime import timedelta

        for timestamp, amount in [
            (start - timedelta(microseconds=1), 1),
            (start, 2),
            (stop - timedelta(microseconds=1), 3),
            (stop, 4),
        ]:
            db.add(
                Payment(
                    invoice_id=records[0].id,
                    amount=amount,
                    currency="INR",
                    payment_date=timestamp,
                    method=PaymentMethod.OTHER,
                    status=PaymentStatus.SUCCESS,
                )
            )
        await db.commit()
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS", json.dumps({"scale": {"token": "scale-only", "company_ids": [str(company_id)]}})
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            clock = perf_counter()
            response = await client.get(
                "/dashboard/analytics",
                params={"start_date": "2026-07-01", "end_date": "2026-09-30", "page_size": 25},
                headers={"Authorization": "Bearer scale-only"},
            )
            elapsed = perf_counter() - clock
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["summary"]["invoice_count"] == 2000
            assert Decimal(result["summary"]["total_invoice_amount"]) == 200000
            assert Decimal(result["summary"]["recovered_amount"]) == 5
            assert sum(Decimal(bucket["recovered_amount"]) for bucket in result["series"]) == 5
            assert len(result["invoices"]) == 25
            assert result["invoice_total"] == 2000
            assert elapsed < 10, "Analytics exceeded the local acceptance performance budget"
            print(f"2,000-row scoped analytics including serialization: {elapsed*1000:.1f} ms")
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Company).where(Company.id == company_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_hosted_financial_reads_fail_closed_without_scope_configuration(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.delenv("ANALYTICS_API_KEYS", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ["/invoices", "/companies", "/recovery-cases", "/dashboard/metrics"]:
            assert (await client.get(path)).status_code == 503
        assert (await client.get("/dashboard/calendar")).status_code == 200
