"""A cursor into a new customer workspace's guided onboarding walkthrough -- Kaveri's deterministic story,
written in one event at a time as the owner plays through it.

Revision ID: b4f1e8a2c637
Revises: a3e5f82c9d16
"""
from alembic import op
import sqlalchemy as sa

revision = "b4f1e8a2c637"
down_revision = "a3e5f82c9d16"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portal_onboarding_progress",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade():
    op.drop_table("portal_onboarding_progress")
