import csv
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.main import app
from app.models import Company, Invoice, Payment
from app.models.enums import CompanySegment, InvoiceStatus, PaymentMethod, PaymentStatus


@pytest.mark.asyncio
async def test_retained_kpi_definitions_and_historical_unavailability(monkeypatch):
    await engine.dispose()
    today = datetime.now(ZoneInfo(settings.business_timezone)).date()
    company_id = None
    try:
        async with async_session_factory() as db:
            company = Company(name="Preservation fixture", segment=CompanySegment.SMB)
            db.add(company)
            await db.flush()
            company_id = company.id
            for total, paid, status in [
                (100, 40, InvoiceStatus.PARTIALLY_PAID),
                (200, 200, InvoiceStatus.PAID),
                (300, 0, InvoiceStatus.CANCELLED),
                (400, 0, InvoiceStatus.WRITTEN_OFF),
            ]:
                invoice = Invoice(
                    company_id=company_id,
                    invoice_number="KEEP-" + uuid4().hex,
                    amount_total=total,
                    amount_paid=paid,
                    currency="INR",
                    posting_date=today,
                    issue_date=today,
                    due_date=today - timedelta(days=30),
                    status=status,
                )
                db.add(invoice)
                await db.flush()
                if paid:
                    db.add(
                        Payment(
                            invoice_id=invoice.id,
                            amount=paid,
                            currency="INR",
                            payment_date=datetime.now(ZoneInfo(settings.business_timezone)),
                            method=PaymentMethod.OTHER,
                            status=PaymentStatus.SUCCESS,
                        )
                    )
            await db.commit()
        monkeypatch.setenv(
            "ANALYTICS_API_KEYS",
            json.dumps({"preserved": {"token": "preserve-fixture", "company_ids": [str(company_id)]}}),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer preserve-fixture"}
            response = await client.get("/dashboard/preserved", headers=headers)
            assert response.status_code == 200, response.text
            data = response.json()
            m = data["metrics"]
            assert Decimal(m["billed"]) == 1000
            assert Decimal(m["invoiced"]) == 700  # cancelled excluded, no redefinition
            assert Decimal(m["open"]) == 60  # written-off and cancelled excluded
            assert Decimal(m["overdue"]) == 60
            assert Decimal(m["overdue_share"]) == 100
            assert Decimal(m["collected"]) == 240
            assert m["open_count"] == 1 and m["scored_open"] == 0
            assert len(data["invoices"]) == 4
            assert sum(Decimal(r["balance"]) for r in data["invoices"]) == Decimal(m["open"])
            assert sum(Decimal(r["amount"]) for r in data["aging"]) == Decimal(m["open"])
            assert sum(Decimal(r["invoiced"]) for r in data["series"]) == Decimal(m["invoiced"])
            assert data["series"][-1]["open"] is not None
            missing = await client.get(
                "/dashboard/preserved", params={"as_of": str(today - timedelta(days=500))}, headers=headers
            )
            assert missing.status_code == 200, missing.text
            assert missing.json()["metrics"]["open"] is None
            assert missing.json()["historical_available"] is False
            filtered = await client.get("/dashboard/preserved", params={"status": "overdue"}, headers=headers)
            assert filtered.json()["total"] == 1
            empty = await client.get("/dashboard/preserved", params={"search": "absent"}, headers=headers)
            assert empty.json()["total"] == 0
            export = await client.get(
                "/dashboard/preserved/export", params={"status": "overdue"}, headers=headers
            )
            assert export.status_code == 200, export.text
            rows = list(csv.DictReader(io.StringIO(export.text.lstrip("\ufeff"))))
            assert len(rows) == 1 and Decimal(rows[0]["balance"]) == 60 and rows[0]["currency"] == "INR"
            assert (await client.get("/dashboard/preserved")).status_code == 401
            assert (
                await client.get("/dashboard/preserved", headers={"Authorization": "Bearer wrong"})
            ).status_code == 403
            assert (
                await client.get("/dashboard/preserved", params={"page_size": 101}, headers=headers)
            ).status_code == 422
    finally:
        if company_id:
            async with async_session_factory() as db:
                await db.execute(delete(Company).where(Company.id == company_id))
                await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_retained_dashboard_benchmark(monkeypatch):
    import statistics
    from time import perf_counter
    from sqlalchemy import text

    await engine.dispose()
    today = datetime.now(ZoneInfo(settings.business_timezone)).date()
    async with async_session_factory() as db:
        company = Company(name="Preservation benchmark", segment=CompanySegment.SMB)
        db.add(company)
        await db.flush()
        cid = company.id
        db.add_all(
            [
                Invoice(
                    company_id=cid,
                    invoice_number="PERF-" + uuid4().hex,
                    amount_total=100,
                    amount_paid=0,
                    currency="INR",
                    posting_date=today,
                    issue_date=today,
                    due_date=today - timedelta(days=1),
                    status=InvoiceStatus.SENT,
                )
                for _ in range(2000)
            ]
        )
        await db.commit()
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS",
        json.dumps({"benchmark": {"token": "preserved-benchmark", "company_ids": [str(cid)]}}),
    )
    from sqlalchemy import event
    slow_queries = {}
    def before(conn, cursor, statement, parameters, context, executemany):
        context._profile_started = perf_counter()
    def after(conn, cursor, statement, parameters, context, executemany):
        elapsed = (perf_counter() - context._profile_started) * 1000
        if elapsed > 100 and statement not in slow_queries and not statement.startswith("EXPLAIN"):
            slow_queries[statement] = elapsed
            print("SLOW_SQL", round(elapsed, 1), statement)
            if len(slow_queries) == 1:
                plan = conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS) " + statement, parameters).scalars().all()
                print("EXACT_SLOW_PLAN\n" + "\n".join(plan))
    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    try:
        measurements = {}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for endpoint in ["/dashboard/analytics", "/dashboard/preserved"]:
                durations = []
                for _ in range(21):
                    began = perf_counter()
                    response = await client.get(
                        endpoint,
                        params={"start_date": str(today), "end_date": str(today)},
                        headers={"Authorization": "Bearer preserved-benchmark"},
                    )
                    durations.append((perf_counter() - began) * 1000)
                    assert response.status_code == 200, response.text
                    data = response.json()
                    if endpoint.endswith("preserved"):
                        assert data["total"] == 2000 and len(data["invoices"]) == 12
                        assert Decimal(data["metrics"]["open"]) == 200000
                measurements[endpoint] = {
                    "cold_ms": round(durations[0], 1),
                    "p50_ms": round(statistics.median(durations[1:]), 1),
                    "p95_ms": round(sorted(durations[1:])[18], 1),
                }
        print(
            "PRESERVATION_BENCHMARK",
            json.dumps({"rows": 2000, "samples": 20, "cache": "none", "measurements": measurements}),
        )
        async with async_session_factory() as db:
            plan = (
                (
                    await db.execute(
                        text(
                            "EXPLAIN (ANALYZE, BUFFERS) SELECT DISTINCT ON (invoice_id) invoice_id, amount_total, amount_paid FROM invoice_state_snapshots WHERE company_id=:cid ORDER BY invoice_id, observed_at DESC, id DESC"
                        ),
                        {"cid": cid},
                    )
                )
                .scalars()
                .all()
            )
            print("PRESERVATION_PLAN\n" + "\n".join(plan))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
        event.remove(engine.sync_engine, "after_cursor_execute", after)
        async with async_session_factory() as db:
            await db.execute(delete(Company).where(Company.id == cid))
            await db.commit()
        await engine.dispose()
