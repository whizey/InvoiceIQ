"""Sample notes for the public showcase: seeded once, idempotent, and the planted note is withheld from the model."""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.agents.guardrails import is_untrusted_instruction
from app.core.db import async_session_factory, engine
from app.models.portal import PortalInvoice, PortalNote, PortalWorkspace
from app.services.portal_recovery import SHOWCASE_BASELINE_SCOPE
from scripts import provision_portal_showcase as provision


@pytest.mark.asyncio
async def test_sample_notes_seed_once_and_include_the_planted_instruction(monkeypatch):
    await engine.dispose()
    workspace_id = uuid.uuid4()
    monkeypatch.setattr(provision, "SHOWCASE_ID", workspace_id)  # never touch a real showcase
    today = date.today()
    try:
        async with async_session_factory() as db:
            db.add(PortalWorkspace(id=workspace_id, name="Seed test", kind="showcase"))
            await db.flush()
            for i in range(60):
                db.add(PortalInvoice(workspace_id=workspace_id, invoice_number=f"S-{i:03d}", counterparty=f"C{i % 7}",
                                     issue_date=today - timedelta(days=40), posting_date=today - timedelta(days=40),
                                     due_date=today - timedelta(days=10), amount_total=Decimal("1000"),
                                     amount_paid=Decimal("1000") if i % 5 == 0 else Decimal("0")))
            await db.flush()
            assert await provision.seed_baseline_notes(db) == 40
            assert await provision.seed_baseline_notes(db) == 0  # idempotent
            await db.commit()
            notes = (await db.scalars(select(PortalNote).where(PortalNote.workspace_id == workspace_id))).all()
            assert len(notes) == 40 and {n.author_scope for n in notes} == {SHOWCASE_BASELINE_SCOPE}
            assert {n.kind for n in notes} == {"customer_message", "dispute", "promise", "internal"}
            planted = [n for n in notes if is_untrusted_instruction(n.body)]
            assert [n.body for n in planted] == [provision.PLANTED_NOTE]
            paid_ids = set((await db.scalars(select(PortalInvoice.id).where(
                PortalInvoice.workspace_id == workspace_id, PortalInvoice.amount_paid >= PortalInvoice.amount_total))).all())
            assert not paid_ids & {n.invoice_id for n in notes}  # notes only on invoices still open
            assert await db.scalar(select(func.count()).select_from(PortalNote).where(
                PortalNote.workspace_id == workspace_id)) == 40
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            await db.commit()
        await engine.dispose()
