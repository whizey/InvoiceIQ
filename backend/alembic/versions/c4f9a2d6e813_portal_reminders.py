"""Log payment reminders people send, for the three-day rule and to see whether payment followed.

Revision ID: c4f9a2d6e813
Revises: b3e8c1d5f702
"""
from alembic import op
import sqlalchemy as sa

revision = "c4f9a2d6e813"
down_revision = "b3e8c1d5f702"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portal_reminders",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("invoice_id", sa.UUID(), sa.ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_scope", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("body", sa.String(2000), nullable=False),
        sa.Column("balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("draft_source", sa.String(64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("channel IN ('email', 'whatsapp')", name="ck_portal_reminders_channel"),
        sa.CheckConstraint("language IN ('english', 'hinglish')", name="ck_portal_reminders_language"),
        sa.CheckConstraint("char_length(body) BETWEEN 10 AND 2000", name="ck_portal_reminders_body_length"),
    )
    op.create_index("ix_portal_reminders_invoice", "portal_reminders", ["workspace_id", "invoice_id", "sent_at"])


def downgrade():
    op.drop_index("ix_portal_reminders_invoice", table_name="portal_reminders")
    op.drop_table("portal_reminders")
