"""One Zoho Books connection per workspace, for the live webhook sync.

Revision ID: a9c4e71bf3d5
Revises: d2a7e4b1c9f6
"""
from alembic import op
import sqlalchemy as sa

revision = "a9c4e71bf3d5"
down_revision = "d2a7e4b1c9f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portal_zoho_connections",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("oauth_state", sa.String(64)),
        sa.Column("oauth_state_created_at", sa.DateTime(timezone=True)),
        sa.Column("zoho_organization_id", sa.String(64)),
        sa.Column("access_token", sa.String(2000)),
        sa.Column("refresh_token", sa.String(2000)),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("webhook_secret", sa.String(64), unique=True),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("invoices_synced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_portal_zoho_connections_state", "portal_zoho_connections", ["oauth_state"])


def downgrade():
    op.drop_index("ix_portal_zoho_connections_state", table_name="portal_zoho_connections")
    op.drop_table("portal_zoho_connections")
