"""Zoho Books live sync: OAuth connect and the webhook payload mapping.

Zoho pushes to us, so this module needs no polling. The access/refresh tokens it stores are not required for
the webhook path itself (Zoho's webhook POST carries the invoice data directly) -- they're kept for a future
initial-backfill pull, so connecting an account does not also require building token-refresh machinery that
nothing here depends on yet.

Zoho's classic Books webhooks are unsigned: the POST body carries no signature to verify. The only authenticity
check available is a secret in the URL itself (see PortalZohoConnection.webhook_secret), never in the body or a
header, since both are attacker-controlled on an unsigned request.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from app.core.config import settings

SCOPE = "ZohoBooks.fullaccess.READ"
SKIP_STATUSES = {"void", "draft"}


def configured() -> bool:
    return bool(settings.zoho_client_id and settings.zoho_client_secret and settings.zoho_redirect_uri)


def authorize_url(state: str) -> str:
    params = {"scope": SCOPE, "client_id": settings.zoho_client_id, "response_type": "code",
              "access_type": "offline", "redirect_uri": settings.zoho_redirect_uri, "state": state,
              "prompt": "consent"}
    return f"{settings.zoho_accounts_domain}/oauth/v2/auth?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Exchange an OAuth authorization code for tokens. Raises ValueError on failure."""
    import httpx

    params = {"grant_type": "authorization_code", "client_id": settings.zoho_client_id,
              "client_secret": settings.zoho_client_secret, "redirect_uri": settings.zoho_redirect_uri,
              "code": code}
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{settings.zoho_accounts_domain}/oauth/v2/token", params=params)
    body = response.json() if response.content else {}
    if response.status_code != 200 or "access_token" not in body:
        raise ValueError(str(body.get("error", "Zoho rejected the authorization code")))
    return body


def _zoho_date(value) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def map_invoice(payload: dict) -> dict:
    """Zoho's invoice-created/updated webhook body -> the fields `upsert_invoice` needs.

    Raises ValueError with a short, plain reason when the payload can't be used -- a status webhook, a
    cancelled invoice, or a shape that isn't an invoice at all.
    """
    invoice = payload.get("invoice") if isinstance(payload, dict) else None
    if not isinstance(invoice, dict):
        raise ValueError("Webhook payload has no invoice object")
    status = str(invoice.get("status", "")).strip().lower()
    if status in SKIP_STATUSES:
        raise ValueError(f"Invoice status is '{status}'; nothing to sync")
    number = str(invoice.get("invoice_number") or "").strip()
    customer = str(invoice.get("customer_name") or "").strip()
    if not number or not customer:
        raise ValueError("Missing invoice number or customer name")
    issue_date = _zoho_date(invoice.get("date"))
    if issue_date is None:
        raise ValueError("Missing or unreadable invoice date")
    due_date = _zoho_date(invoice.get("due_date")) or issue_date + timedelta(days=30)
    try:
        total = Decimal(str(invoice.get("total", "0"))).quantize(Decimal("0.01"))
        balance = Decimal(str(invoice.get("balance", invoice.get("total", "0")))).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("Invoice amount is not a number") from None
    if total <= 0:
        raise ValueError("Invoice total is zero or negative")
    paid = total - balance
    if paid < 0 or paid > total or due_date < issue_date:
        raise ValueError("Balance, due date or total is inconsistent with the invoice")
    return {"invoice_number": number[:64], "counterparty": customer[:160], "issue_date": issue_date,
            "due_date": due_date, "amount_total": total, "amount_paid": paid}


async def upsert_invoice(db, workspace_id, mapped: dict):
    """Create or update the one invoice this webhook describes. Returns (invoice, created, newly_paid)."""
    from sqlalchemy import select

    from app.models.portal import PortalInvoice, PortalPayment

    existing = await db.scalar(select(PortalInvoice).where(
        PortalInvoice.workspace_id == workspace_id, PortalInvoice.invoice_number == mapped["invoice_number"]))
    created = existing is None
    previously_paid = existing.amount_paid if existing else Decimal(0)
    if existing is None:
        existing = PortalInvoice(workspace_id=workspace_id, invoice_number=mapped["invoice_number"],
                                 issue_date=mapped["issue_date"], posting_date=mapped["issue_date"])
        db.add(existing)
    existing.counterparty = mapped["counterparty"]
    existing.due_date = mapped["due_date"]
    existing.amount_total = mapped["amount_total"]
    existing.amount_paid = mapped["amount_paid"]
    await db.flush()
    newly_paid = mapped["amount_paid"] - previously_paid
    if newly_paid > 0:
        db.add(PortalPayment(workspace_id=workspace_id, invoice_id=existing.id,
                             paid_on=date.today(), amount=newly_paid))
    return existing, created, newly_paid > 0
