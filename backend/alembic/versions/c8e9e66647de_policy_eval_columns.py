"""policy eval columns

Revision ID: c8e9e66647de
Revises: d43c032f30e9
Create Date: 2026-09-05 14:57:58.004397

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c8e9e66647de'
down_revision: Union[str, Sequence[str], None] = 'd43c032f30e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # recovery_action_type enum already exists (created for action_type in
    # the initial migration) — create_type=False so this doesn't try to
    # CREATE TYPE a second time.
    recovery_action_type = postgresql.ENUM(
        'SEND_EMAIL', 'SEND_PAYMENT_LINK', 'TRACK_PROMISE_TO_PAY', 'ESCALATE', 'WAIT', 'CLOSE_CASE',
        name='recovery_action_type',
        create_type=False,
    )
    op.add_column(
        'recovery_actions',
        sa.Column('recommended_action_type', recovery_action_type, nullable=True),
    )
    op.add_column(
        'policy_decisions',
        sa.Column('rule', sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('policy_decisions', 'rule')
    op.drop_column('recovery_actions', 'recommended_action_type')
