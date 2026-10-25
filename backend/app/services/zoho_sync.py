"""Zoho Books live sync: OAuth connect, the one-time invoice backfill, and the webhook payload mapping.

Going forward, Zoho pushes to us, so the webhook path needs no polling. The access/refresh tokens are used for
the "Sync Now" backfill (pulling invoices that already existed in Zoho before the webhook was connected,
which a webhook -- firing only on future create/update events -- can never retroactively deliver on its own).

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
    # Always accounts.zoho.com: Zoho requires every authorization request to start there
    # regardless of the account's actual data centre, and routes internally from there.
    params = {"scope": SCOPE, "client_id": settings.zoho_client_id, "response_type": "code",
              "access_type": "offline", "redirect_uri": settings.zoho_redirect_uri, "state": state,
              "prompt": "consent"}
    return f"https://accounts.zoho.com/oauth/v2/auth?{urlencode(params)}"


async def exchange_code(code: str, accounts_server: str | None) -> dict:
    """Exchange an OAuth authorization code for tokens, against the data centre that issued it
    (Zoho's callback reports this as `accounts-server`; a code is only valid at its own data
    centre's token endpoint). Raises ValueError on failure."""
    import httpx

    server = (accounts_server or settings.zoho_accounts_domain).rstrip("/")
    params = {"grant_type": "authorization_code", "client_id": settings.zoho_client_id,
              "client_secret": settings.zoho_client_secret, "redirect_uri": settings.zoho_redirect_uri,
              "code": code}
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{server}/oauth/v2/token", params=params)
    body = response.json() if response.content else {}
    if response.status_code != 200 or "access_token" not in body:
        raise ValueError(str(body.get("error", "Zoho rejected the authorization code")))
    return body


async def refresh_access_token(refresh_token: str, accounts_server: str) -> dict:
    """A fresh access token from a stored refresh token, against the same data centre the connection was
    established at -- a refresh token is no more portable across data centres than an authorization code.
    Raises ValueError on failure."""
    import httpx

    params = {"grant_type": "refresh_token", "client_id": settings.zoho_client_id,
              "client_secret": settings.zoho_client_secret, "refresh_token": refresh_token}
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{accounts_server.rstrip('/')}/oauth/v2/token", params=params)
    body = response.json() if response.content else {}
    if response.status_code != 200 or "access_token" not in body:
        raise ValueError(str(body.get("error", "Zoho rejected the refresh token")))
    return body


async def list_organizations(access_token: str, api_domain: str) -> list[dict]:
    """The Zoho Books organizations this access token can see. Raises ValueError on failure."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"{api_domain.rstrip('/')}/books/v3/organizations",
                                    headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
    body = response.json() if response.content else {}
    if response.status_code != 200:
        raise ValueError(str(body.get("message", "Could not list Zoho organizations")))
    return body.get("organizations") or []


MAX_BACKFILL_INVOICES = 2000  # matches ledger_import.py's own per-workspace import cap
INVOICES_PER_PAGE = 200  # Zoho's documented maximum


async def list_invoices(access_token: str, api_domain: str, organization_id: str) -> list[dict]:
    """Every invoice in the organization, up to MAX_BACKFILL_INVOICES -- a one-time pull for invoices that
    already existed before the webhook was connected (a webhook only ever fires on future events, so it can
    never retroactively deliver these on its own). Raises ValueError on failure."""
    import httpx

    invoices: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            response = await client.get(f"{api_domain.rstrip('/')}/books/v3/invoices",
                params={"organization_id": organization_id, "page": page, "per_page": INVOICES_PER_PAGE},
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
            body = response.json() if response.content else {}
            if response.status_code != 200:
                raise ValueError(str(body.get("message", "Could not list Zoho invoices")))
            invoices.extend(body.get("invoices") or [])
            context = body.get("page_context") or {}
            if not context.get("has_more_page") or len(invoices) >= MAX_BACKFILL_INVOICES:
                break
            page += 1
    return invoices[:MAX_BACKFILL_INVOICES]


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
    zoho_invoice_id = str(invoice.get("invoice_id") or "").strip() or None
    return {"invoice_number": number[:64], "counterparty": customer[:160], "issue_date": issue_date,
            "due_date": due_date, "amount_total": total, "amount_paid": paid, "zoho_invoice_id": zoho_invoice_id}


async def upsert_invoice(db, workspace_id, mapped: dict):
    """Create or update the one invoice this webhook describes. Returns (invoice, created, newly_paid).

    A row that already has at least one real payment record (see upsert_payment) is trusted to carry its own
    accurate payment history, so this does not also infer a synthetic one from the balance moving -- that
    fallback exists only for a workspace with no payment records synced, where it's the only signal at all.
    """
    from sqlalchemy import exists, select

    from app.models.portal import PortalInvoice, PortalPayment

    existing = await db.scalar(select(PortalInvoice).where(
        PortalInvoice.workspace_id == workspace_id, PortalInvoice.invoice_number == mapped["invoice_number"]))
    created = existing is None
    previously_paid = existing.amount_paid if existing else Decimal(0)
    has_real_payments = existing is not None and bool(await db.scalar(select(exists().where(
        PortalPayment.invoice_id == existing.id, PortalPayment.zoho_payment_id.isnot(None)))))
    if existing is None:
        existing = PortalInvoice(workspace_id=workspace_id, invoice_number=mapped["invoice_number"],
                                 issue_date=mapped["issue_date"], posting_date=mapped["issue_date"])
        db.add(existing)
    existing.counterparty = mapped["counterparty"]
    existing.due_date = mapped["due_date"]
    existing.amount_total = mapped["amount_total"]
    existing.amount_paid = mapped["amount_paid"]
    existing.zoho_invoice_id = mapped.get("zoho_invoice_id") or existing.zoho_invoice_id
    await db.flush()
    newly_paid = mapped["amount_paid"] - previously_paid
    if newly_paid > 0 and not has_real_payments:
        db.add(PortalPayment(workspace_id=workspace_id, invoice_id=existing.id,
                             paid_on=date.today(), amount=newly_paid))
    return existing, created, newly_paid > 0


def map_payment(payload: dict) -> dict:
    """Zoho's payment-created/updated webhook body (the same full-object shape as a single GET or create
    response) -> the fields `upsert_payment` needs. Raises ValueError when the payload can't be used."""
    payment = payload.get("payment") if isinstance(payload, dict) else None
    if not isinstance(payment, dict):
        raise ValueError("Webhook payload has no payment object")
    payment_id = str(payment.get("payment_id") or "").strip()
    if not payment_id:
        raise ValueError("Missing payment id")
    paid_on = _zoho_date(payment.get("date"))
    if paid_on is None:
        raise ValueError("Missing or unreadable payment date")
    allocations = []
    for line in payment.get("invoices") or []:
        zoho_invoice_id = str(line.get("invoice_id") or "").strip()
        try:
            applied = Decimal(str(line.get("amount_applied", "0"))).quantize(Decimal("0.01"))
        except InvalidOperation:
            continue
        if zoho_invoice_id and applied > 0:
            allocations.append({"zoho_invoice_id": zoho_invoice_id, "amount": applied})
    if not allocations:
        raise ValueError("Payment is not allocated to any invoice")
    return {"zoho_payment_id": payment_id, "paid_on": paid_on, "allocations": allocations}


async def _reconcile_invoice_paid(db, invoice) -> None:
    """Recompute an invoice's own amount_paid from the sum of its real (Zoho-sourced) payment records, so it
    stays correct regardless of whether the Invoice webhook, the Payment webhook, or both are configured --
    the payment ledger is the source of truth once any real payment exists for the invoice."""
    from sqlalchemy import func, select

    from app.models.portal import PortalPayment

    total = await db.scalar(select(func.coalesce(func.sum(PortalPayment.amount), 0)).where(
        PortalPayment.invoice_id == invoice.id, PortalPayment.zoho_payment_id.isnot(None)))
    invoice.amount_paid = min(invoice.amount_total, total)


async def upsert_payment(db, workspace_id, mapped: dict) -> dict:
    """Record each of a payment's invoice allocations as its own dated PortalPayment row, idempotently (a
    retried or re-synced delivery of the same payment updates nothing new), and bring each affected invoice's
    own amount_paid in line with its real payment history. An allocation against an invoice we haven't synced
    yet (the payment webhook arriving before the invoice one, or a different data source entirely) is counted
    as unmatched rather than failing the whole payment.
    Returns {"applied": int, "unmatched": int}."""
    from sqlalchemy import select

    from app.models.portal import PortalInvoice, PortalPayment

    applied = unmatched = 0
    for allocation in mapped["allocations"]:
        invoice = await db.scalar(select(PortalInvoice).where(
            PortalInvoice.workspace_id == workspace_id,
            PortalInvoice.zoho_invoice_id == allocation["zoho_invoice_id"]))
        if invoice is None:
            unmatched += 1
            continue
        row = await db.scalar(select(PortalPayment).where(
            PortalPayment.workspace_id == workspace_id, PortalPayment.invoice_id == invoice.id,
            PortalPayment.zoho_payment_id == mapped["zoho_payment_id"]))
        if row is None:
            db.add(PortalPayment(workspace_id=workspace_id, invoice_id=invoice.id, paid_on=mapped["paid_on"],
                                 amount=allocation["amount"], zoho_payment_id=mapped["zoho_payment_id"]))
        else:
            row.paid_on, row.amount = mapped["paid_on"], allocation["amount"]
        await db.flush()
        await _reconcile_invoice_paid(db, invoice)
        applied += 1
    await db.flush()
    return {"applied": applied, "unmatched": unmatched}


async def list_payments(access_token: str, api_domain: str, organization_id: str) -> list[dict]:
    """Every customer payment in the organization, up to MAX_BACKFILL_INVOICES. The list endpoint gives a
    single invoice_number per payment rather than the full per-invoice allocation array a webhook delivers
    (that detail is only in the single-payment GET), so a backfilled payment split across more than one
    invoice in Zoho is only reconciled against the one the list reports. Raises ValueError on failure."""
    import httpx

    payments: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            response = await client.get(f"{api_domain.rstrip('/')}/books/v3/customerpayments",
                params={"organization_id": organization_id, "page": page, "per_page": INVOICES_PER_PAGE},
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
            body = response.json() if response.content else {}
            if response.status_code != 200:
                raise ValueError(str(body.get("message", "Could not list Zoho payments")))
            payments.extend(body.get("customer_payments") or [])
            context = body.get("page_context") or {}
            if not context.get("has_more_page") or len(payments) >= MAX_BACKFILL_INVOICES:
                break
            page += 1
    return payments[:MAX_BACKFILL_INVOICES]


def map_backfilled_payment(raw: dict) -> dict:
    """One row from the `list_payments` backfill (a lighter shape than the webhook's) -> the fields
    `upsert_backfilled_payment` needs, with a single-invoice allocation inferred from `invoice_number` since
    the list endpoint carries no invoice_id or allocation array. Raises ValueError when unusable."""
    payment_id = str(raw.get("payment_id") or "").strip()
    number = str(raw.get("invoice_number") or "").strip()
    if not payment_id or not number:
        raise ValueError("Missing payment id or invoice number")
    paid_on = _zoho_date(raw.get("date"))
    if paid_on is None:
        raise ValueError("Missing or unreadable payment date")
    try:
        amount = Decimal(str(raw.get("amount", "0"))).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("Payment amount is not a number") from None
    if amount <= 0:
        raise ValueError("Payment amount is zero or negative")
    return {"zoho_payment_id": payment_id, "paid_on": paid_on, "invoice_number": number, "amount": amount}


async def upsert_backfilled_payment(db, workspace_id, mapped: dict) -> bool:
    """Record one backfilled payment against the invoice its invoice_number matches, idempotently, and bring
    that invoice's amount_paid in line. Returns whether it matched a synced invoice (false means unmatched,
    not an error -- the invoice may not exist in this workspace, e.g. a different organization, or wasn't
    itself backfilled)."""
    from sqlalchemy import select

    from app.models.portal import PortalInvoice, PortalPayment

    invoice = await db.scalar(select(PortalInvoice).where(
        PortalInvoice.workspace_id == workspace_id, PortalInvoice.invoice_number == mapped["invoice_number"]))
    if invoice is None:
        return False
    row = await db.scalar(select(PortalPayment).where(
        PortalPayment.workspace_id == workspace_id, PortalPayment.invoice_id == invoice.id,
        PortalPayment.zoho_payment_id == mapped["zoho_payment_id"]))
    if row is None:
        db.add(PortalPayment(workspace_id=workspace_id, invoice_id=invoice.id, paid_on=mapped["paid_on"],
                             amount=mapped["amount"], zoho_payment_id=mapped["zoho_payment_id"]))
    else:
        row.paid_on, row.amount = mapped["paid_on"], mapped["amount"]
    await db.flush()
    await _reconcile_invoice_paid(db, invoice)
    await db.flush()
    return True
