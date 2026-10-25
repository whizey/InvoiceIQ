"""The live "watch it happen" simulation: a visitor's own isolated, ephemeral copy of Kaveri's deterministic
story, written into the database one event at a time instead of all at once, through the same real import and
scoring pipeline every other invoice and payment goes through.

Deliberately reuses app/seed/showcase_seed.py's existing, already-reviewed generator rather than writing a
second one: the story is still Kaveri Industrial Supplies, just revealed progressively instead of in bulk, and
the same (today) always produces the same events in the same order -- a reproducible scenario, not a random one.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

MAX_EVENTS = 200  # a bounded, predictable playback length, not the whole year the full seed covers


def build_events(today: date) -> list[dict]:
    """Kaveri's invoice-and-payment story, merged into one chronological event list, capped at MAX_EVENTS.
    Pure and cheap enough to call on every tick: showcase_seed.build() does no I/O."""
    from app.seed import showcase_seed

    ledger = showcase_seed.build(today)
    accounts = {i["number"]: i["account"] for i in ledger.invoices}
    events = [{"at": i["posted"], "kind": "invoice", "number": i["number"], "account": i["account"],
              "total": i["total"], "due": i["due"]} for i in ledger.invoices]
    events += [{"at": p["paid_on"], "kind": "payment", "number": p["number"], "amount": p["amount"],
               "account": accounts.get(p["number"])} for p in ledger.payments]
    events.sort(key=lambda e: e["at"])
    return events[:MAX_EVENTS]


async def apply_event(db, workspace_id, event: dict) -> None:
    """Write one event into the database for real: a new invoice row, or a payment against one already
    written earlier in this same session's playback. A payment whose invoice fell outside MAX_EVENTS (its
    own creation never got played) is silently skipped -- nothing to pay against, not an error."""
    from sqlalchemy import select

    from app.models.portal import PortalInvoice, PortalPayment

    if event["kind"] == "invoice":
        db.add(PortalInvoice(workspace_id=workspace_id, invoice_number=event["number"],
                             counterparty=event["account"], issue_date=event["at"], posting_date=event["at"],
                             due_date=event["due"], amount_total=event["total"], amount_paid=Decimal(0)))
    else:
        invoice = await db.scalar(select(PortalInvoice).where(
            PortalInvoice.workspace_id == workspace_id, PortalInvoice.invoice_number == event["number"]))
        if invoice is None:
            return
        db.add(PortalPayment(workspace_id=workspace_id, invoice_id=invoice.id, paid_on=event["at"],
                             amount=event["amount"]))
        invoice.amount_paid = invoice.amount_paid + event["amount"]
    await db.flush()
