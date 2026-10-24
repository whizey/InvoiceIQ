"""Log each Zoho webhook delivery and its outcome, so a skipped invoice is visible in the product.

Revision ID: c7b4d9e2a815
Revises: f1e6a3c9d402
"""
from alembic import op
import sqlalchemy as sa

revision = "c7b4d9e2a815"
down_revision = "f1e6a3c9d402"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portal_zoho_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("invoice_number", sa.String(64)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_portal_zoho_events_workspace", "portal_zoho_events", ["workspace_id", "created_at"])


def downgrade():
    op.drop_index("ix_portal_zoho_events_workspace", table_name="portal_zoho_events")
    op.drop_table("portal_zoho_events")
