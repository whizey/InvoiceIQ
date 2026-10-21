"""Metadata-only traces of assistant agent runs.

Revision ID: 9c3e5a1f7b42
Revises: e4b1c9d83f26
"""
from alembic import op
import sqlalchemy as sa

revision = "9c3e5a1f7b42"
down_revision = "e4b1c9d83f26"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assistant_traces",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('llm', 'tool', 'check', 'policy', 'fallback', 'run')",
                           name="ck_assistant_traces_kind"),
    )
    op.create_index("ix_assistant_traces_workspace_time", "assistant_traces", ["workspace_id", "occurred_at"])
    op.create_index("ix_assistant_traces_run", "assistant_traces", ["run_id"])


def downgrade():
    op.drop_index("ix_assistant_traces_run", table_name="assistant_traces")
    op.drop_index("ix_assistant_traces_workspace_time", table_name="assistant_traces")
    op.drop_table("assistant_traces")
