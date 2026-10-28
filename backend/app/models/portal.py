"""Customer workspaces and the isolated public workspace.

Portal ledger records are deliberately separate from legacy Company/Invoice.
No foreign key or cascade points from a showcase record into customer accounting.
"""
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class PortalWorkspace(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "portal_workspaces"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # customer | showcase
    # A historical ledger is viewed as of a fixed date (the public 2016 dataset); None means today.
    as_of: Mapped[date | None] = mapped_column(Date)

    def today(self) -> date:
        return self.as_of or date.today()

    def now(self) -> datetime:
        """Current time, or the end of the as-of business day for a historical ledger."""
        return datetime.combine(self.as_of, time(18), tzinfo=timezone.utc) if self.as_of else datetime.now(timezone.utc)


class PortalUser(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "portal_users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)


class PortalMembership(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "portal_memberships"
    __table_args__ = (UniqueConstraint("user_id", "workspace_id", name="uq_portal_membership"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class PortalSession(Base):
    __tablename__ = "portal_sessions"
    __table_args__ = (Index("ix_portal_sessions_expires", "expires_at"),)

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalInvoice(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "portal_invoices"
    __table_args__ = (
        UniqueConstraint("workspace_id", "invoice_number", name="uq_portal_invoice_number"),
        Index("ix_portal_invoice_workspace_due", "workspace_id", "due_date"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty: Mapped[str] = mapped_column(String(160), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    risk_probability: Mapped[float | None] = mapped_column(Numeric(7, 6))
    risk_provenance: Mapped[str | None] = mapped_column(String(80))
    # Zoho's own invoice id (never the invoice_number, which is just a display label) -- the stable key a
    # Zoho payment's allocation line refers back to, so a payment can be matched to the right row even if
    # the invoice number were ever edited.
    zoho_invoice_id: Mapped[str | None] = mapped_column(String(64))


class PortalCaseEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "portal_case_events"
    __table_args__ = (Index("ix_portal_case_events_scope", "workspace_id", "actor_scope", "invoice_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False)
    actor_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PortalPayment(Base, UUIDPrimaryKeyMixin):
    """Dated, reconciled payment movement; never infer a payment date -- except the CSV importer's and Zoho's
    own balance-delta fallback, which predate this rule and still mark their guesses in a warning rather than
    pretending to know the real date. A row with `zoho_payment_id` set came from a real Zoho payment record
    (webhook or Sync Now), not a guess."""
    __tablename__ = "portal_payments"
    __table_args__ = (
        Index("ix_portal_payment_scoped_date", "workspace_id", "paid_on"),
        UniqueConstraint("workspace_id", "zoho_payment_id", "invoice_id", name="uq_portal_payment_zoho_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False
    )
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Set only for a payment synced from Zoho (one row per invoice this payment was allocated against, since
    # one Zoho payment can be split across several invoices); null for every other import path.
    zoho_payment_id: Mapped[str | None] = mapped_column(String(64))


class PortalNote(Base, UUIDPrimaryKeyMixin):
    """A dated note on an invoice: a customer message, dispute, payment promise or internal remark.

    Notes are free text written by people (or seeded with the public workspace); the assistant reads them as
    untrusted data. Showcase visitors see the shared baseline plus their own, like review decisions.
    """
    __tablename__ = "portal_notes"
    __table_args__ = (Index("ix_portal_notes_invoice", "workspace_id", "invoice_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # customer_message | dispute | promise | internal
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    author_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Semantic search: the note's embedding as float32 bytes, and the model that produced it (None until embedded).
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    embedding_model: Mapped[str | None] = mapped_column(String(64))


class PortalReminder(Base, UUIDPrimaryKeyMixin):
    """A payment reminder a person sent themselves (by email or WhatsApp), logged so the three-day rule holds
    and so we can see whether a payment followed. Showcase visitors see only their own, like decisions."""
    __tablename__ = "portal_reminders"
    __table_args__ = (Index("ix_portal_reminders_invoice", "workspace_id", "invoice_id", "sent_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False)
    actor_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # email | whatsapp
    language: Mapped[str] = mapped_column(String(16), nullable=False)  # english | hinglish
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    draft_source: Mapped[str] = mapped_column(String(64), nullable=False)  # model name, or "template"
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PortalZohoConnection(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One Zoho Books organization streaming invoices into a workspace by webhook."""
    __tablename__ = "portal_zoho_connections"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Set on /connect, cleared once /callback succeeds; a stale one past OAUTH_STATE_MAX_AGE is rejected.
    oauth_state: Mapped[str | None] = mapped_column(String(64))
    oauth_state_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zoho_organization_id: Mapped[str | None] = mapped_column(String(64))
    access_token: Mapped[str | None] = mapped_column(String(2000))
    refresh_token: Mapped[str | None] = mapped_column(String(2000))
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The regional API base Zoho's token response reports (e.g. www.zohoapis.in) -- read from the response,
    # never guessed from a setting, since it follows the account's own data centre.
    api_domain: Mapped[str | None] = mapped_column(String(120))
    # The accounts-server the authorization code was issued from -- a refresh_token is only redeemable there,
    # same reasoning as api_domain.
    accounts_server: Mapped[str | None] = mapped_column(String(120))
    webhook_secret: Mapped[str | None] = mapped_column(String(64), unique=True)
    webhook_signing_secret: Mapped[str | None] = mapped_column(String(50))
    webhook_signature_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invoices_synced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PortalZohoWebhookDelivery(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "portal_zoho_webhook_deliveries"
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("workspace_id", "digest"),)


class PortalZohoEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One Zoho webhook delivery and what happened to it -- a draft invoice or an unreadable payload is
    recorded here, not just silently dropped, so it shows up in the product instead of only in Zoho's own
    webhook logs."""
    __tablename__ = "portal_zoho_events"
    __table_args__ = (Index("ix_portal_zoho_events_workspace", "workspace_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # created | updated | skipped
    reason: Mapped[str | None] = mapped_column(String(300))


class PortalOnboardingProgress(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A cursor into a brand-new customer workspace's guided "watch it fill up" onboarding walkthrough --
    Kaveri's own deterministic story, written into the real workspace one event at a time as the owner plays
    through it, through the same real import and scoring pipeline every other invoice goes through, rather
    than being loaded all at once. Deleted along with the workspace, like everything else in it."""
    __tablename__ = "portal_onboarding_progress"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
