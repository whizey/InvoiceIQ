"""Add isolated portal accounts, tenant ledgers and auditable case events.

Revision ID: 6b4e8d2f091a
Revises: 0d7a9e3b14c2
"""
from alembic import op
import sqlalchemy as sa

revision = "6b4e8d2f091a"
down_revision = "0d7a9e3b14c2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portal_workspaces",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('customer', 'showcase')", name="portal_workspace_kind"),
    )
    op.create_table(
        "portal_users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("full_name", sa.String(160), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "portal_memberships",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "workspace_id", name="uq_portal_membership"),
    )
    op.create_table(
        "portal_sessions",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("portal_users.id", ondelete="CASCADE")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_portal_sessions_expires", "portal_sessions", ["expires_at"])
    op.create_table(
        "portal_invoices",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invoice_number", sa.String(64), nullable=False),
        sa.Column("counterparty", sa.String(160), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(14, 2), nullable=False),
        sa.Column("risk_probability", sa.Numeric(7, 6)),
        sa.Column("risk_provenance", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "invoice_number", name="uq_portal_invoice_number"),
        sa.CheckConstraint("amount_total >= 0 AND amount_paid >= 0 AND amount_paid <= amount_total", name="portal_invoice_amounts"),
        sa.CheckConstraint("risk_probability IS NULL OR (risk_probability >= 0 AND risk_probability <= 1)", name="portal_invoice_probability"),
    )
    op.create_index("ix_portal_invoice_workspace_due", "portal_invoices", ["workspace_id", "due_date"])
    op.create_table(
        "portal_case_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invoice_id", sa.UUID(), sa.ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_scope", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("decision IN ('APPROVE', 'REJECT', 'ESCALATE', 'RESET')", name="portal_case_decision"),
    )
    op.create_index("ix_portal_case_events_scope", "portal_case_events",
                    ["workspace_id", "actor_scope", "invoice_id", "created_at"])


def downgrade():
    op.drop_index("ix_portal_case_events_scope", table_name="portal_case_events")
    op.drop_table("portal_case_events")
    op.drop_index("ix_portal_invoice_workspace_due", table_name="portal_invoices")
    op.drop_table("portal_invoices")
    op.drop_index("ix_portal_sessions_expires", table_name="portal_sessions")
    op.drop_table("portal_sessions")
    op.drop_table("portal_memberships")
    op.drop_table("portal_users")
    op.drop_table("portal_workspaces")
