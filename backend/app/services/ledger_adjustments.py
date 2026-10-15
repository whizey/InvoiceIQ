"""Dated source adjustments for the existing read-only receivable audit.

Credit notes and write-offs reduce receivables, never increase collected cash.
Disputes are memorandum entries and do not extinguish the debt. No source
adjustments are inferred from an invoice's current status.
"""
from decimal import Decimal


def adjustment_totals(row, cutoff, posted, *, money, day):
    totals = {key: Decimal("0.00") for key in (
        "credit_notes", "debit_adjustments", "writeoffs", "cancellations", "disputed",
    )}
    kinds = {"CREDIT_NOTE": "credit_notes", "DEBIT_ADJUSTMENT": "debit_adjustments",
             "WRITE_OFF": "writeoffs", "CANCELLATION": "cancellations", "DISPUTE": "disputed"}
    seen = set()
    references = []
    for entry in row.get("adjustments", []):
        identity = entry.get("id")
        if (not identity or identity in seen or not entry.get("source_reference")
                or not row.get("company_id") or entry.get("company_id") != row["company_id"]
                or entry.get("invoice_id") != row["id"] or entry.get("currency") != row["currency"]
                or entry.get("kind") not in kinds):
            raise ValueError("Invalid adjustment provenance or scope")
        seen.add(identity)
        effective = day(entry.get("effective_date"), "adjustment.effective_date")
        amount = money(entry.get("amount"), "adjustment.amount")
        if effective < posted:
            raise ValueError("Adjustment predates invoice")
        if effective <= cutoff:
            totals[kinds[entry["kind"]]] += amount
            references.append(entry["source_reference"])
    return totals, references
