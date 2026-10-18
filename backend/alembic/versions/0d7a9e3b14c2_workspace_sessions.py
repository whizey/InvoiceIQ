"""Add revocable workspace sessions without storing bearer tokens.

Revision ID: 0d7a9e3b14c2
Revises: f7c0b57c9e11
"""

from alembic import op
import sqlalchemy as sa

revision = "0d7a9e3b14c2"
down_revision = "f7c0b57c9e11"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_sessions",
        sa.Column("token_digest", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_workspace_sessions_expires", "workspace_sessions", ["expires_at"])


def downgrade():
    op.drop_index("ix_workspace_sessions_expires", table_name="workspace_sessions")
    op.drop_table("workspace_sessions")
