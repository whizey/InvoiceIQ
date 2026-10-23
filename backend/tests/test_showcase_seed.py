"""The public workspace's business: believable figures, a consistent story per customer, safe provisioning."""
import asyncio
import uuid
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.agents.guardrails import is_untrusted_instruction
from app.core.db import async_session_factory, engine
from app.models.portal import PortalInvoice, PortalNote, PortalWorkspace
from app.seed import showcase_seed
from scripts import provision_portal_showcase as provision

TODAY = date(2026, 10, 23)


def test_the_ledger_is_believable_for_a_small_distributor():
    ledger = showcase_seed.build(TODAY)
    m = showcase_seed.summary(ledger, TODAY)
    assert 500 <= m["invoices"] <= 900 and m["customers"] == 40
    assert Decimal(2_00_00_000) <= m["invoiced"] <= Decimal(8_00_00_000)  # ₹2–8 crore a year
    assert Decimal(25_000) <= m["median_invoice"] <= Decimal(80_000)
    assert 15 <= m["overdue_share_pct"] <= 50
    assert all(Decimal(5_000) <= i["total"] <= Decimal(4_50_000) for i in ledger.invoices)
    assert showcase_seed.summary(showcase_seed.build(TODAY), TODAY) == m  # deterministic


def test_each_customer_tells_one_consistent_story():
    ledger = showcase_seed.build(TODAY)
    by_number = {i["number"]: i for i in ledger.invoices}
    overdue_habits = Counter(i["habit"] for i in ledger.invoices if i["paid"] < i["total"] and i["due"] < TODAY)
    assert overdue_habits["reliable"] == 0  # reliable payers are never the ones overdue
    for note in ledger.notes:
        habit = by_number[note["number"]]["habit"]
        if note["kind"] == "dispute":
            assert by_number[note["number"]]["disputed"]
        if "cash is tight" in note["body"]:
            assert habit == "struggling"
    for decision in ledger.decisions:
        invoice = by_number[decision["number"]]
        assert invoice["paid"] < invoice["total"] and invoice["due"] < TODAY
        if decision["decision"] == "REJECT":
            assert invoice["disputed"]
    disputed = [i for i in ledger.invoices if i["disputed"]]
    assert disputed and all(i["habit"] == "disputing" and i["paid"] == 0 for i in disputed)
    assert len({i["account"] for i in disputed}) == len(disputed)  # one disputed invoice per customer
    planted = [n for n in ledger.notes if is_untrusted_instruction(n["body"])]
    assert [n["body"] for n in planted] == [showcase_seed.PLANTED_NOTE]


@pytest.mark.asyncio
async def test_provisioning_seeds_refreshes_and_replaces_only_the_showcase(monkeypatch):
    await engine.dispose()
    showcase_id = uuid.uuid4()
    monkeypatch.setattr(provision, "SHOWCASE_ID", showcase_id)  # never touch a real showcase
    try:
        async with async_session_factory() as db:
            db.add(PortalWorkspace(id=showcase_id, name="old", kind="showcase"))
            await db.flush()
            assert await provision.needs_rebuild(db, TODAY) == "empty"
            result = await provision.seed_ledger(db, TODAY)
            assert result["scored"] == result["invoices"] and result["notes"] > 20
            assert await provision.needs_rebuild(db, TODAY) is None
            assert await provision.needs_rebuild(db, TODAY + timedelta(days=45)) == "stale data"
            db.add(PortalInvoice(workspace_id=showcase_id, invoice_number="IQ-2026-00001", counterparty="Account 001",
                                 issue_date=TODAY, posting_date=TODAY, due_date=TODAY, amount_total=Decimal(1),
                                 amount_paid=Decimal(0)))
            await db.flush()
            assert await provision.needs_rebuild(db, TODAY) == "earlier data"
            await provision.clear_showcase(db)
            for model in (PortalInvoice, PortalNote):
                assert await db.scalar(select(func.count()).select_from(model).where(
                    model.workspace_id == showcase_id)) == 0
            customer_id = uuid.uuid4()
            db.add(PortalWorkspace(id=customer_id, name="Real customer", kind="customer"))
            await db.flush()
            monkeypatch.setattr(provision, "SHOWCASE_ID", customer_id)
            with pytest.raises(RuntimeError, match="not the showcase"):
                await provision.clear_showcase(db)
            await db.rollback()
    finally:
        async with async_session_factory() as db:
            from sqlalchemy import delete
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id.in_([showcase_id])))
            await db.commit()
        await engine.dispose()


def test_rebuilding_is_kept_away_from_startup_without_an_explicit_target(monkeypatch):
    monkeypatch.delenv("INVOICEIQ_PORTAL_SHOWCASE", raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(provision.main())
