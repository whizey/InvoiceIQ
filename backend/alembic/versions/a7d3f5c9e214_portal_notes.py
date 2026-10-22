"""Notes on portal invoices: customer messages, disputes, payment promises and internal remarks.

Revision ID: a7d3f5c9e214
Revises: 9c3e5a1f7b42
"""
from alembic import op
import sqlalchemy as sa

revision = "a7d3f5c9e214"
down_revision = "9c3e5a1f7b42"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portal_notes",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("invoice_id", sa.UUID(), sa.ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("body", sa.String(1000), nullable=False),
        sa.Column("author_scope", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('customer_message', 'dispute', 'promise', 'internal')",
                           name="ck_portal_notes_kind"),
        sa.CheckConstraint("char_length(body) BETWEEN 3 AND 1000", name="ck_portal_notes_body_length"),
    )
    op.create_index("ix_portal_notes_invoice", "portal_notes", ["workspace_id", "invoice_id", "created_at"])


def downgrade():
    op.drop_index("ix_portal_notes_invoice", table_name="portal_notes")
    op.drop_table("portal_notes")
