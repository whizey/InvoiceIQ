"""Consumer bookkeeping: processed events, latest case risk, approval alerts and dead letters.

Revision ID: c4a91e6d2b75
Revises: a02849ab1830
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "c4a91e6d2b75"
down_revision = "a02849ab1830"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.String(60), primary_key=True),
        sa.Column("event_id", sa.UUID(), primary_key=True),
        sa.Column("topic", sa.String(100), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "case_risk_latest",
        sa.Column("case_id", sa.UUID(), primary_key=True),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("risk_score", sa.Float()),
        sa.Column("recovery_probability", sa.Float()),
        sa.Column("drivers", pg.JSONB()),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_case_risk_level", "case_risk_latest", ["risk_level"])
    op.create_table(
        "approval_alerts",
        sa.Column("event_id", sa.UUID(), primary_key=True),
        sa.Column("case_id", sa.UUID(), nullable=False),
        sa.Column("action_id", sa.String(64)),
        sa.Column("action_type", sa.String(60)),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_approval_alerts_case_id", "approval_alerts", ["case_id"])
    op.create_table(
        "dead_letter_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("consumer", sa.String(60), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("topic", sa.String(100), nullable=False),
        sa.Column("payload", pg.JSONB()),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("consumer", "event_id", name="uq_dead_letter_event"),
    )


def downgrade():
    op.drop_table("dead_letter_events")
    op.drop_index("ix_approval_alerts_case_id", table_name="approval_alerts")
    op.drop_table("approval_alerts")
    op.drop_index("ix_case_risk_level", table_name="case_risk_latest")
    op.drop_table("case_risk_latest")
    op.drop_table("processed_events")
