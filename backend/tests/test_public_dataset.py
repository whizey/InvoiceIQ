"""SQL parity against the raw CSV, isolated from development and public data."""

from datetime import datetime, timezone
from decimal import Decimal
import httpx
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import insert, select, func, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.models import Company, Invoice, Payment, DatasetImport
from app.models.enums import CompanySegment, InvoiceStatus, PaymentMethod, PaymentStatus
from app.seed.load_real_dataset import (
    identity,
    read_dataset,
    require_isolated_url,
    dataset_metadata,
    resolve_dataset_path,
)


@pytest_asyncio.fixture
async def corpus(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    raw = read_dataset()
    async with factory() as session, session.begin():
        # This corpus runs in the verified suite database; remove earlier seed
        # rows so the existing exact CSV parity assertions remain meaningful.
        from app.models.base import Base

        for table in reversed(Base.metadata.sorted_tables):
            # The observed-history coverage marker belongs to the migration,
            # not to this corpus fixture. Removing it makes later real as-of
            # queries correctly report history unavailable on suite reruns.
            if table.name == "analytics_history_state":
                continue
            await session.execute(delete(table))
        if await session.get(DatasetImport, "public_b2b_dataset") is None:
            import hashlib

            metadata = dataset_metadata(raw)
            session.add(
                DatasetImport(
                    source="public_b2b_dataset",
                    source_sha256=hashlib.sha256(resolve_dataset_path().read_bytes()).hexdigest(),
                    dataset_as_of=datetime.fromisoformat(metadata["dataset_as_of"]).date(),
                    row_count=metadata["row_count"],
                    customer_count=metadata["customer_count"],
                    amount_total=Decimal(metadata["amount_total"]),
                    reconciled_at=datetime.now(timezone.utc),
                )
            )
        if not await session.scalar(select(func.count(Invoice.id))):
            await session.execute(
                insert(Company),
                [
                    {
                        "id": identity("customer", int(c)),
                        "name": f"Customer {i:04d}",
                        "segment": CompanySegment.SMB,
                    }
                    for i, c in enumerate(sorted(raw.Cust_Num.unique()))
                ],
            )
            invoices, payments = [], []
            for r in raw.itertuples(index=False):
                iid = identity("invoice", r.Document_No)
                invoices.append(
                    {
                        "id": iid,
                        "company_id": identity("customer", int(r.Cust_Num)),
                        "invoice_number": f"B2B-{r.Document_No}",
                        "amount_total": int(r.Amount),
                        "amount_paid": int(r.Amount),
                        "currency": "INR",
                        "issue_date": r.Doc_Date.date(),
                        "posting_date": r.Posting_Date.date(),
                        "due_date": r.Net_Due_Date.date(),
                        "status": InvoiceStatus.PAID,
                    }
                )
                payments.append(
                    {
                        "id": identity("payment", r.Document_No),
                        "invoice_id": iid,
                        "amount": int(r.Amount),
                        "currency": "INR",
                        "payment_date": datetime.combine(
                            r.Clearing_date.date(), datetime.min.time(), tzinfo=timezone.utc
                        ),
                        "method": PaymentMethod.OTHER,
                        "status": PaymentStatus.SUCCESS,
                    }
                )
            for offset in range(0, len(invoices), 500):
                await session.execute(insert(Invoice), invoices[offset : offset + 500])
                await session.execute(insert(Payment), payments[offset : offset + 500])

    async def db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = db
    try:
        yield raw
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,column", [("invoiced", "Posting_Date"), ("collected", "Clearing_date")])
@pytest.mark.parametrize("grain", ["month", "year"])
async def test_financial_parity(corpus, endpoint, column, grain):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(f"/analytics/{endpoint}?grain={grain}&start_date=2011-01-01&end_date=2016-12-31")
    assert r.status_code == 200, r.text
    actual = r.json()
    expected = corpus.groupby(corpus[column].dt.to_period("M" if grain == "month" else "Y")).Amount.agg(
        ["sum", "count"]
    )
    assert actual["row_count"] == len(corpus)
    assert sum(Decimal(r["amount"]) for r in actual["series"]) == Decimal(int(corpus.Amount.sum()))
    for r in actual["series"]:
        key = r["period"][:7] if grain == "month" else r["period"][:4]
        assert Decimal(r["amount"]) == Decimal(int(expected.loc[key, "sum"]))
        assert r["count"] == int(expected.loc[key, "count"])


@pytest.mark.asyncio
async def test_other_aggregates_and_security(corpus):
    end = pd.Timestamp("2015-12-31")
    cohort = corpus[corpus.Posting_Date.between("2015-01-01", end)]
    overdue = cohort[(cohort.Net_Due_Date < end) & (cohort.Clearing_date > end)].copy()
    overdue["bucket"] = pd.cut(
        (end - overdue.Net_Due_Date).dt.days,
        [-1, 30, 60, 90, float("inf")],
        labels=["0–30", "31–60", "61–90", "90+"],
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        q = "start_date=2015-01-01&end_date=2015-12-31"
        r = await c.get("/analytics/overdue-aging?" + q)
        assert r.status_code == 200, r.text
        for row in r.json()["series"]:
            expected = overdue[overdue.bucket == row["bucket"]]
            assert row["count"] == len(expected)
            assert Decimal(row["amount"]) == Decimal(int(expected.Amount.sum()))
        rates = (await c.get("/analytics/late-rate?grain=year&" + q)).json()["series"]
        assert (
            abs(float(rates[0]["actual_late_rate"]) - (cohort.Clearing_date > cohort.Net_Due_Date).mean())
            < 1e-12
        )
        assert (await c.get("/analytics/risk-distribution?" + q)).json()["series"] == [
            {"risk": "unscored", "count": len(cohort)}
        ]
        for name in ["policy-actions", "overrides"]:
            r = await c.get("/analytics/" + name + "?" + q)
            assert r.status_code == 200
            assert r.json()["row_count"] == 0
        assert (
            await c.get("/analytics/invoiced?start_date=2016-01-01&end_date=2015-01-01")
        ).status_code == 422
        assert (await c.get("/analytics/invoiced?start_date=2020-01-01&end_date=2020-12-31")).json()[
            "row_count"
        ] == 0
        assert (await c.post("/invoices", json={})).status_code == 403
        assert (await c.delete("/anything")).status_code == 403
        assert (
            await c.get("/analytics/metadata", headers={"origin": "https://attacker.invalid"})
        ).status_code == 403


def test_import_safety():
    assert identity("invoice", 123) == identity("invoice", 123)
    assert identity("invoice", 123) != identity("payment", 123)
    with pytest.raises(ValueError):
        require_isolated_url("postgresql://localhost/recovery_db")
