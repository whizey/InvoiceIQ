"""Durable recovery agent node spans.

Revision ID: f7c0b57c9e11
Revises: e05f7b9a812d
"""

from alembic import op
import sqlalchemy as sa

revision = "f7c0b57c9e11"
down_revision = "e05f7b9a812d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_node_spans",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "recovery_case_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                "recovery_cases.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey(
                "companies.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "investigation_id",
            sa.Uuid(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "node_name",
            sa.String(64),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.String(16),
            nullable=False,
        ),
        sa.Column(
            "duration_ms",
            sa.Float(),
            nullable=False,
        ),
        sa.Column(
            "policy_decision",
            sa.String(32),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name="ck_agent_node_spans_duration",
        ),
    )

    op.create_index(
        "ix_agent_spans_company_time",
        "agent_node_spans",
        ["company_id", "occurred_at"],
    )

    op.create_index(
        "ix_agent_spans_case_investigation",
        "agent_node_spans",
        [
            "recovery_case_id",
            "investigation_id",
            "occurred_at",
        ],
    )


def downgrade():
    op.drop_index(
        "ix_agent_spans_case_investigation",
        table_name="agent_node_spans",
    )

    op.drop_index(
        "ix_agent_spans_company_time",
        table_name="agent_node_spans",
    )

    op.drop_table("agent_node_spans")
