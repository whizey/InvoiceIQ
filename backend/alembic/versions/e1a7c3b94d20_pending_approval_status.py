"""pending approval status

Revision ID: e1a7c3b94d20
Revises: d52b0b687e9e
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e1a7c3b94d20"
down_revision: Union[str, Sequence[str], None] = "d52b0b687e9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE recovery_action_status ADD VALUE IF NOT EXISTS 'PENDING_APPROVAL'")


def downgrade() -> None:
    # Postgres cannot drop an enum value without rebuilding the type.
    pass
