"""Payment reminders: the model drafts, a person edits and sends, and the system keeps the rules and the log.

A draft may use only the case's facts. Any number in it that is not one of those facts rejects the draft; after
one retry the fixed template is used instead. Policy: no reminder on a disputed invoice, no second contact within
three days, and a large or very late balance is flagged for a manager to send.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.agents.grounding import check_grounding, grounded_values

COOL_OFF_DAYS = 3
ESCALATE_AMOUNT = Decimal(500_000)
ESCALATE_DAYS = 60
OUTCOME_DAYS = 14  # a payment within this many days of a reminder counts as following it
CHANNELS = ("email", "whatsapp")
LANGUAGES = ("english", "hinglish")


def rupees(value) -> str:
    """Indian grouping: ₹1,20,000."""
    whole = int(Decimal(value).quantize(Decimal(1)))
    text = str(abs(whole))
    if len(text) > 3:
        head, tail = text[:-3], text[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        text = ",".join([head, *groups, tail]) if head else ",".join([*groups, tail])
    return "₹" + text


def facts(row, business: str, today: date) -> dict:
    balance = row.amount_total - row.amount_paid
    return {"business": business, "customer": row.counterparty, "invoice_number": row.invoice_number,
            "invoice_date": row.posting_date.strftime("%d %b %Y"), "due_date": row.due_date.strftime("%d %b %Y"),
            "invoice_amount": rupees(row.amount_total), "amount_paid": rupees(row.amount_paid),
            "balance": rupees(balance), "days_overdue": max((today - row.due_date).days, 0),
            "part_paid": row.amount_paid > 0}


def policy(row, reason_kind: str | None, last_contact: datetime | None, now: datetime) -> dict:
    balance = row.amount_total - row.amount_paid
    days = (now.date() - row.due_date).days
    blocked = None
    if balance <= 0:
        blocked = "This invoice is paid."
    elif reason_kind == "dispute":
        blocked = "On hold: the customer disputes this invoice. Resolve the dispute before chasing payment."
    elif last_contact is not None and now - last_contact < timedelta(days=COOL_OFF_DAYS):
        next_day = (last_contact + timedelta(days=COOL_OFF_DAYS)).date()
        blocked = f"Contacted on {last_contact.date():%d %b}. The next reminder can go on {next_day:%d %b}."
    escalate = None
    if balance >= ESCALATE_AMOUNT:
        escalate = f"Over {rupees(ESCALATE_AMOUNT)} outstanding: a manager should send this."
    elif days > ESCALATE_DAYS:
        escalate = f"More than {ESCALATE_DAYS} days overdue: a manager should send this."
    return {"allowed": blocked is None, "blocked": blocked, "escalate": escalate}


def template(f: dict, channel: str, language: str) -> str:
    late = f"{f['days_overdue']} days overdue" if f["days_overdue"] else f"due on {f['due_date']}"
    paid = f" Thank you for the {f['amount_paid']} already received." if f["part_paid"] else ""
    if language == "hinglish":
        body = (f"Namaste {f['customer']} team, invoice {f['invoice_number']} ka {f['balance']} abhi baaki hai "
                f"(due date {f['due_date']}, ab {late}).{paid} Kripya payment kab tak ho jayega, bata dijiye. "
                f"Dhanyavaad, {f['business']}")
    else:
        body = (f"Dear {f['customer']} team, this is a reminder that {f['balance']} is outstanding on invoice "
                f"{f['invoice_number']}, {late} (due {f['due_date']}).{paid} Could you let us know when we can "
                f"expect payment? Thank you, {f['business']}")
    if channel == "email":
        return f"Subject: Payment reminder: invoice {f['invoice_number']}\n\n{body}"
    return body


def prompt(f: dict, channel: str, language: str) -> str:
    style = ("a short, polite email with a 'Subject:' line" if channel == "email"
             else "a short, polite WhatsApp message of at most four sentences, no subject line")
    tongue = ("Hinglish (Hindi in Latin script mixed with English, as Indian businesses write on WhatsApp)"
              if language == "hinglish" else "plain Indian business English")
    return (f"Write {style} in {tongue} reminding a customer about an unpaid invoice. Use only these facts, "
            f"exactly as written, and no other numbers, dates, account details or links:\n"
            f"- From: {f['business']}\n- To: {f['customer']}\n- Invoice: {f['invoice_number']}, dated "
            f"{f['invoice_date']}, due {f['due_date']}\n- Invoice amount: {f['invoice_amount']}\n"
            f"- Already paid: {f['amount_paid']}\n- Still owed: {f['balance']}\n- Days overdue: {f['days_overdue']}\n"
            f"Ask when payment can be expected. Do not threaten, do not mention interest or legal action. "
            f"Reply with the message only.")


def stray_figures(draft: str, f: dict) -> list[str]:
    """Numbers in the draft that are not among the case's facts (dates are compared as text, not figures)."""
    report = check_grounding(draft, grounded_values(f, *[str(v) for v in f.values()]), exempt_small=False)
    return [c.text for c in report.ungrounded]


async def draft(f: dict, channel: str, language: str, clients, timeout: float = 25) -> dict:
    """The first client whose draft uses only the facts wins; otherwise the template. Says which it was."""
    from langchain_core.messages import HumanMessage

    message = [HumanMessage(prompt(f, channel, language))]
    rejected: list[str] = []
    for client, name in clients:
        for _ in range(2):  # one retry per model: a fresh draft often drops the stray figure
            try:
                reply = await asyncio.wait_for(client.ainvoke(message), timeout=timeout)
            except Exception:  # noqa: BLE001 - provider down or rate-limited: try the next one
                break
            text = reply.content if isinstance(reply.content, str) else "".join(
                p.get("text", "") for p in reply.content if isinstance(p, dict))
            text = text.strip()
            stray = stray_figures(text, f)
            if text and not stray:
                return {"body": text, "source": name, "rejected": rejected}
            rejected.extend(stray or ["empty draft"])
    return {"body": template(f, channel, language), "source": "template", "rejected": rejected}


def outcome(sent_at: datetime, payments) -> dict | None:
    """The first payment on the invoice within OUTCOME_DAYS after the reminder, if any."""
    start = sent_at.astimezone(timezone.utc).date()
    after = sorted((p for p in payments if start <= p.paid_on <= start + timedelta(days=OUTCOME_DAYS)),
                   key=lambda p: p.paid_on)
    if not after:
        return None
    return {"paid_on": str(after[0].paid_on), "amount": str(after[0].amount), "days_after": (after[0].paid_on - start).days}
