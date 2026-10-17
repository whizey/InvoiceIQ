"""Scope approval alerts to a workspace and track when they are resolved.

Revision ID: d7e2c5f19a40
Revises: c4a91e6d2b75
"""
from alembic import op
import sqlalchemy as sa

revision = "d7e2c5f19a40"
down_revision = "c4a91e6d2b75"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approval_alerts", sa.Column("workspace_id", sa.UUID()))
    op.add_column("approval_alerts", sa.Column("visibility_scope", sa.String(80)))
    op.add_column("approval_alerts", sa.Column("subject", sa.String(240)))
    op.add_column("approval_alerts", sa.Column("status", sa.String(12), nullable=False, server_default="OPEN"))
    op.add_column("approval_alerts", sa.Column("resolved_decision", sa.String(12)))
    op.add_column("approval_alerts", sa.Column("resolved_at", sa.DateTime(timezone=True)))
    op.create_index("ix_approval_alerts_scope", "approval_alerts", ["workspace_id", "visibility_scope", "status"])


def downgrade():
    op.drop_index("ix_approval_alerts_scope", table_name="approval_alerts")
    for column in ("resolved_at", "resolved_decision", "status", "subject", "visibility_scope", "workspace_id"):
        op.drop_column("approval_alerts", column)
