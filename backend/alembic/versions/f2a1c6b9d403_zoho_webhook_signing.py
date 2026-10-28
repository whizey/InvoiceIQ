"""Add per-connection Zoho Books webhook signature migration state.

Revision ID: f2a1c6b9d403
Revises: b4f1e8a2c637
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a1c6b9d403"
down_revision = "b4f1e8a2c637"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portal_zoho_connections", sa.Column("webhook_signing_secret", sa.String(50)))
    op.add_column("portal_zoho_connections", sa.Column("webhook_signature_required", sa.Boolean(),
        nullable=False, server_default=sa.false()))
    op.create_table("portal_zoho_webhook_deliveries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "digest"))


def downgrade():
    op.drop_table("portal_zoho_webhook_deliveries")
    op.drop_column("portal_zoho_connections", "webhook_signature_required")
    op.drop_column("portal_zoho_connections", "webhook_signing_secret")
