"""Record when a dead-lettered event was successfully replayed.

Revision ID: e4b1c9d83f26
Revises: d7e2c5f19a40
"""
from alembic import op
import sqlalchemy as sa

revision = "e4b1c9d83f26"
down_revision = "d7e2c5f19a40"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("dead_letter_events", sa.Column("replayed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_dead_letter_pending", "dead_letter_events", ["replayed_at"])


def downgrade():
    op.drop_index("ix_dead_letter_pending", table_name="dead_letter_events")
    op.drop_column("dead_letter_events", "replayed_at")
