"""Customer workspaces and the isolated public workspace.

Portal ledger records are deliberately separate from legacy Company/Invoice.
No foreign key or cascade points from a showcase record into customer accounting.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, LargeBinary, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class PortalWorkspace(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "portal_workspaces"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # customer | showcase


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
    """Dated, reconciled payment movement; never infer a payment date."""
    __tablename__ = "portal_payments"
    __table_args__ = (Index("ix_portal_payment_scoped_date", "workspace_id", "paid_on"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False
    )
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)


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
