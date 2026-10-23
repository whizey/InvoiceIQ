"""Explicit, idempotent provisioning into a NEW dedicated showcase workspace.

Never run this script as part of app startup or during customer registration.
The caller must validate the target database and deliberately set
INVOICEIQ_PORTAL_SHOWCASE=true. It cannot touch legacy accounting tables.
"""
import asyncio
import argparse
import os
import uuid
from datetime import date, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from app.core.db import async_session_factory, engine
from app.api.portal import SHOWCASE_ID
from app.models.event_effects import ApprovalAlert
from app.models.portal import PortalWorkspace, PortalInvoice, PortalPayment, PortalCaseEvent, PortalNote
from app.seed import showcase_seed
from app.services import ledger_import
from app.services import note_embeddings
from app.services.portal_recovery import SHOWCASE_BASELINE_SCOPE
from app.services.portal_risk import score_workspace

STALE_AFTER_DAYS = 30  # a ledger whose newest invoice is older than this is rebuilt, so it never looks abandoned


def validate_target():
    if os.getenv("INVOICEIQ_PORTAL_SHOWCASE", "").lower() != "true":
        raise RuntimeError("Set INVOICEIQ_PORTAL_SHOWCASE=true explicitly")
    database = make_url(os.environ["DATABASE_URL"]).database
    allowed = {"invoiceiq_sample", "invoiceiq_sample_test", "invoiceiq_portal_test"}
    # A deployment may deliberately name one extra database (for example `invoiceiq`) by
    # setting INVOICEIQ_SHOWCASE_DATABASE to exactly that name; the default stays strict.
    explicit = os.getenv("INVOICEIQ_SHOWCASE_DATABASE", "").strip()
    if explicit:
        allowed.add(explicit)
    if database not in allowed:
        raise RuntimeError("STOP: showroom provisioning is restricted to disposable sample databases")


def _id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(SHOWCASE_ID, f"ledger-v2:{kind}:{key}")


async def needs_rebuild(db, today: date) -> str | None:
    """Why the showcase should be (re)built, or None when it is current."""
    count = await db.scalar(select(func.count()).select_from(PortalInvoice).where(
        PortalInvoice.workspace_id == SHOWCASE_ID))
    if not count:
        return "empty"
    legacy = await db.scalar(select(func.count()).select_from(PortalInvoice).where(
        PortalInvoice.workspace_id == SHOWCASE_ID,
        PortalInvoice.invoice_number.notlike(f"{showcase_seed.NUMBER_PREFIX}-%")))
    if legacy:
        return "earlier data"
    newest = await db.scalar(select(func.max(PortalInvoice.posting_date)).where(PortalInvoice.workspace_id == SHOWCASE_ID))
    if newest is None or newest < today - timedelta(days=STALE_AFTER_DAYS):
        return "stale data"
    return None


async def clear_showcase(db) -> None:
    """Remove every showcase record (visitors' practice decisions and notes included). Showcase only."""
    workspace = await db.get(PortalWorkspace, SHOWCASE_ID)
    if workspace is None or workspace.kind != "showcase":
        raise RuntimeError("Refusing to clear a workspace that is not the showcase")
    await db.execute(delete(ApprovalAlert).where(ApprovalAlert.workspace_id == SHOWCASE_ID))
    for model in (PortalNote, PortalCaseEvent, PortalPayment, PortalInvoice):
        await db.execute(delete(model).where(model.workspace_id == SHOWCASE_ID))
    await db.flush()


async def seed_ledger(db, today: date) -> dict:
    """Write the business for today, score it with the verified model and embed its notes.

    The invoices go in the way a customer's would: as a Zoho Books export read by the same importer.
    """
    ledger = showcase_seed.build(today)
    imported = ledger_import.prepare("kaveri-zoho-books.csv", showcase_seed.zoho_export(ledger, today).encode(),
                                     today=today)
    if imported.skipped or imported.warnings or len(imported.rows) != len(ledger.invoices):
        raise RuntimeError(f"Importer rejected the generated export: {imported.report()['skipped']}")
    ids = {}
    for row in imported.rows:
        invoice = PortalInvoice(id=_id("invoice", row["invoice_number"]), workspace_id=SHOWCASE_ID,
                                invoice_number=row["invoice_number"], counterparty=row["counterparty"],
                                issue_date=row["issue_date"], posting_date=row["posting_date"],
                                due_date=row["due_date"], amount_total=row["amount_total"],
                                amount_paid=row["amount_paid"])
        db.add(invoice)
        ids[row["invoice_number"]] = invoice.id
    await db.flush()  # invoices before the rows that reference them
    for row in imported.rows:
        if row["amount_paid"] > 0:
            db.add(PortalPayment(id=_id("payment", row["invoice_number"]), workspace_id=SHOWCASE_ID,
                                 invoice_id=ids[row["invoice_number"]], paid_on=row["payment_date"],
                                 amount=row["amount_paid"]))
    for index, row in enumerate(ledger.decisions):
        db.add(PortalCaseEvent(id=_id("decision", str(index)), workspace_id=SHOWCASE_ID,
                               invoice_id=ids[row["number"]], actor_scope=SHOWCASE_BASELINE_SCOPE,
                               decision=row["decision"], note=row["note"], created_at=row["at"]))
    for index, row in enumerate(ledger.notes):
        db.add(PortalNote(id=_id("note", str(index)), workspace_id=SHOWCASE_ID, invoice_id=ids[row["number"]],
                          kind=row["kind"], body=row["body"], author_scope=SHOWCASE_BASELINE_SCOPE,
                          created_at=row["at"]))
    await db.flush()
    scoring = await score_workspace(db, SHOWCASE_ID)
    embedded = await note_embeddings.backfill(db, SHOWCASE_ID)
    return {**showcase_seed.summary(ledger, today), "scored": scoring["scored"],
            "model_version": scoring["model_version"], "notes_embedded": embedded}


async def main(*, refresh: bool = False):
    validate_target()
    if refresh and make_url(os.environ["DATABASE_URL"]).database != "invoiceiq_sample_test":
        raise RuntimeError("Forced refresh is restricted to the disposable sample test database")
    today = date.today()
    try:
        async with async_session_factory() as db:
            workspace = await db.get(PortalWorkspace, SHOWCASE_ID)
            if workspace and workspace.kind != "showcase":
                raise RuntimeError("Existing workspace is not a showcase")
            if not workspace:
                workspace = PortalWorkspace(id=SHOWCASE_ID, name=showcase_seed.BUSINESS, kind="showcase")
                db.add(workspace)
                await db.flush()
            workspace.name = showcase_seed.BUSINESS
            reason = "forced refresh" if refresh else await needs_rebuild(db, today)
            if reason is None:
                embedded = await note_embeddings.backfill(db, SHOWCASE_ID)
                await db.commit()
                print(f"Showcase is current; {embedded} notes embedded for semantic search")
                return
            await clear_showcase(db)
            result = await seed_ledger(db, today)
            await db.commit()
            print(f"Showcase rebuilt ({reason}): {result['invoices']} invoices for {result['customers']} customers, "
                  f"{result['decisions']} decisions, {result['notes']} notes, {result['scored']} scored "
                  f"({result['model_version']}), {result['notes_embedded']} notes embedded")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Update only the verified disposable sample test showcase")
    asyncio.run(main(refresh=parser.parse_args().refresh))
