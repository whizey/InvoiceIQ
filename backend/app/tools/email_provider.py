"""Real email delivery for the "reminder to my inbox" demo feature.

Mirrors app/events/publisher.py's shape exactly: a Protocol, a real
implementation, a simulated fallback, and a config-keyed factory. The
destination is always settings.demo_notify_email — this module has no path
that can send anywhere else — and a delivery failure is logged, never
raised, matching every other optional-integration in this app (LLM, Kafka,
Redis all fail soft).

send_reminder_email() is the ONLY call site that ever invokes a real
provider. The LangGraph workflow (app/agents/graph.py) and the scheduler
(app/services/scheduler.py) both continue to call the always-mock
app/tools/mock_tools.send_email() instead, so neither can ever trigger a
real send on their own.
"""

import logging
from datetime import datetime, timezone
from typing import Literal, Protocol
from app.core.time import utc_today

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import CommunicationLog, Contact, Invoice, RecoveryCase
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationStatus,
)

logger = logging.getLogger("app.email")

SendStatus = Literal["SENT", "FAILED"]


class EmailProvider(Protocol):
    async def send(self, *, to: str, subject: str, body: str, html: str | None = None) -> SendStatus: ...


class ResendEmailProvider:
    """Real delivery via Resend (https://resend.com/docs/api-reference/emails/send-email)."""

    _ENDPOINT = "https://api.resend.com/emails"

    def __init__(self, api_key: str, from_address: str) -> None:
        self._api_key = api_key
        self._from = from_address

    async def send(self, *, to: str, subject: str, body: str, html: str | None = None) -> SendStatus:
        import httpx

        payload: dict = {"from": self._from, "to": [to], "subject": subject, "text": body}
        if html:
            payload["html"] = html

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    self._ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
                response.raise_for_status()
            return "SENT"
        except Exception:
            logger.warning("failed to send reminder email via Resend to=%s", to, exc_info=True)
            return "FAILED"


class SimulatedEmailProvider:
    """Fallback used when RESEND_API_KEY isn't configured — no network call."""

    async def send(self, *, to: str, subject: str, body: str, html: str | None = None) -> SendStatus:
        logger.info("email (no Resend configured) to=%s subject=%s", to, subject)
        return "FAILED"


def get_email_provider() -> EmailProvider:
    # Deliberately NOT cached (unlike app.events.get_publisher(), which caches
    # a real persistent Kafka connection worth reusing) — both providers here
    # are cheap, stateless value objects, and re-reading settings on every
    # call is what lets tests monkeypatch resend_api_key per-test and get the
    # right provider back immediately.
    if settings.resend_api_key and settings.resend_from:
        return ResendEmailProvider(settings.resend_api_key, settings.resend_from)
    return SimulatedEmailProvider()


async def send_reminder_email(
    session: AsyncSession,
    case: RecoveryCase,
    invoice: Invoice,
    contact: Contact | None,
    company_name: str,
) -> dict:
    """Send (or simulate) exactly one reminder email to settings.demo_notify_email.

    Never sends to `contact.email` — that address is only used to describe,
    in the body, who this would notionally be for in a real system.
    Callers must check settings.demo_notify_email is set before calling this
    (see the API layer, which errors clearly instead of silently no-op'ing).

    company_name is passed explicitly (not read off case.invoice.company)
    to avoid a lazy-load in this async session — callers already have the
    company loaded as a flat object, same as app/agents/graph.py's nodes do.
    """
    to = settings.demo_notify_email
    assert to, "send_reminder_email requires settings.demo_notify_email to be set"

    contact_name = contact.name if contact else "there"
    greeting_name = contact_name.split()[0] if contact else "there"
    days_overdue = max((utc_today() - invoice.due_date).days, 0)
    due_date_str = invoice.due_date.strftime("%d %b %Y")
    amount_str = f"₹{invoice.amount_total:,.2f}"

    subject = f"Payment Reminder — Invoice {invoice.invoice_number} ({amount_str})"

    disclosure = (
        f'Portfolio demo notice: "{contact_name}" and "{company_name}" are fictional test data. '
        f"This email is always delivered to {to}, the developer's own inbox — never to a real "
        f"recipient — regardless of which case triggers it."
    )

    body = (
        f"Hi {greeting_name},\n\n"
        f"This is a reminder that Invoice {invoice.invoice_number} for {amount_str} was due on "
        f"{due_date_str} and is now {days_overdue} day(s) overdue.\n\n"
        f"Could you let us know the expected payment date, or reply if this has already been settled?\n\n"
        f"Invoice Number: {invoice.invoice_number}\n"
        f"Company: {company_name}\n"
        f"Amount Due: {amount_str}\n"
        f"Due Date: {due_date_str}\n\n"
        f"Thank you,\n"
        f"AI Revenue Recovery Team\n\n"
        f"---\n"
        f"{disclosure}"
    )

    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;color:#1e293b;font-size:14px;line-height:1.5;">
  <p>Hi {greeting_name},</p>
  <p>This is a reminder that <strong>Invoice {invoice.invoice_number}</strong> for <strong>{amount_str}</strong>
  was due on {due_date_str} and is now <strong>{days_overdue} day(s) overdue</strong>.</p>
  <p>Could you let us know the expected payment date, or reply if this has already been settled?</p>
  <table style="width:100%;border-collapse:collapse;margin:20px 0;">
    <tr><td style="padding:6px 0;color:#64748b;">Invoice Number</td><td style="padding:6px 0;text-align:right;"><strong>{invoice.invoice_number}</strong></td></tr>
    <tr><td style="padding:6px 0;color:#64748b;">Company</td><td style="padding:6px 0;text-align:right;">{company_name}</td></tr>
    <tr><td style="padding:6px 0;color:#64748b;">Amount Due</td><td style="padding:6px 0;text-align:right;"><strong>{amount_str}</strong></td></tr>
    <tr><td style="padding:6px 0;color:#64748b;">Due Date</td><td style="padding:6px 0;text-align:right;">{due_date_str}</td></tr>
  </table>
  <p>Thank you,<br/>AI Revenue Recovery Team</p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0;"/>
  <p style="font-size:12px;color:#94a3b8;">{disclosure}</p>
</div>"""

    provider = get_email_provider()
    is_configured = bool(settings.resend_api_key and settings.resend_from)
    status = await provider.send(to=to, subject=subject, body=body, html=html) if is_configured else "FAILED"

    # Three outcomes, three statuses. This used to collapse to
    #   SENT if status == "SENT" else SIMULATED
    # which recorded a REAL send failure as a simulation: with a valid
    # RESEND_API_KEY and Resend rejecting the request (422 on an unverified
    # sender, say), the log said SIMULATED and the API told the operator
    # "simulated (no email provider configured)". A configured-but-broken
    # provider was indistinguishable from an absent one, which is the
    # failure you most need to see. CommunicationStatus.FAILED existed and
    # was never written anywhere in the app.
    if status == "SENT":
        log_status = CommunicationStatus.SENT
    elif is_configured:
        log_status = CommunicationStatus.FAILED
    else:
        log_status = CommunicationStatus.SIMULATED

    session.add(
        CommunicationLog(
            recovery_case_id=case.id,
            contact_id=contact.id if contact else None,
            channel=CommunicationChannel.EMAIL,
            direction=CommunicationDirection.OUTBOUND,
            subject=subject,
            body=body,
            status=log_status,
            recipient_email=to,
            sent_at=datetime.now(timezone.utc),
        )
    )

    return {"channel": "email", "to": to, "status": log_status.value, "simulated": log_status == CommunicationStatus.SIMULATED}
