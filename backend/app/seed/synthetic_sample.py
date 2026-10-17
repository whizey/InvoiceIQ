"""Reconcile and identify the dedicated synthetic portfolio dataset."""

import asyncio
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.models import AgentDecision, Company, DatasetImport, Invoice, Payment
from app.seed.data import COMPANIES

MARKER = "synthetic_portfolio_sample_v1"
SOURCE = Path(__file__).with_name("data.py")


def source_digest() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


async def synthetic_sample_scope(db, *, allow_legacy_labels: bool = False) -> tuple:
    marker = await db.get(DatasetImport, MARKER)
    if marker is None or marker.source_sha256 != source_digest():
        raise RuntimeError("Synthetic sample provenance is missing or stale")
    companies = (await db.execute(select(Company.id, Company.name))).all()
    if len(companies) != len(COMPANIES) or {name for _, name in companies} != {spec.name for spec in COMPANIES}:
        raise RuntimeError("Synthetic sample company scope does not match its source")
    count, amount, paid = (await db.execute(select(
        func.count(Invoice.id), func.coalesce(func.sum(Invoice.amount_total), 0),
        func.coalesce(func.sum(Invoice.amount_paid), 0),
    ))).one()
    payment_total = await db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)))
    if (count != marker.row_count or Decimal(amount) != marker.amount_total
            or Decimal(paid) != Decimal(payment_total)):
        raise RuntimeError("Synthetic sample financial reconciliation failed")
    names = set((await db.scalars(select(AgentDecision.model_name).distinct())).all())
    allowed = {"synthetic_fixture", "gpt-4o-mini"} if allow_legacy_labels else {"synthetic_fixture"}
    if not names.issubset(allowed):
        raise RuntimeError("Synthetic sample contains unverified agent provenance")
    return tuple(cid for cid, _ in companies)


async def correct_fixture_decision_provenance() -> None:
    from sqlalchemy.engine import make_url

    if make_url(settings.database_url).database not in {"invoiceiq_sample", "invoiceiq_sample_test"}:
        raise RuntimeError("Decision provenance correction is restricted to dedicated sample databases")
    async with async_session_factory() as db:
        await synthetic_sample_scope(db, allow_legacy_labels=True)
        await db.execute(update(AgentDecision).where(AgentDecision.model_name == "gpt-4o-mini")
                         .values(model_name="synthetic_fixture"))
        await db.commit()
        await synthetic_sample_scope(db)


async def stamp_sample() -> None:
    async with async_session_factory() as db:
        if await db.get(DatasetImport, MARKER) is not None:
            raise RuntimeError("Synthetic sample provenance already exists; refusing reseed")
        companies = (await db.execute(select(Company.id, Company.name))).all()
        if len(companies) != len(COMPANIES) or {name for _, name in companies} != {spec.name for spec in COMPANIES}:
            raise RuntimeError("Seeded companies do not match the synthetic source")
        count, amount, paid = (await db.execute(select(
            func.count(Invoice.id), func.coalesce(func.sum(Invoice.amount_total), 0),
            func.coalesce(func.sum(Invoice.amount_paid), 0),
        ))).one()
        payment_total = await db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)))
        if count < len(COMPANIES) or Decimal(paid) != Decimal(payment_total):
            raise RuntimeError("Synthetic invoice and payment totals do not reconcile")
        db.add(DatasetImport(
            source=MARKER, source_sha256=source_digest(),
            dataset_as_of=datetime.now(timezone.utc).date(), row_count=count,
            customer_count=len(companies), amount_total=amount,
            reconciled_at=datetime.now(timezone.utc),
        ))
        await db.commit()
        await synthetic_sample_scope(db)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(stamp_sample())
    print("Synthetic sample provenance and invoice/payment totals verified")
