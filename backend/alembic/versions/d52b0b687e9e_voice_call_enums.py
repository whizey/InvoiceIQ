"""voice call enums

Revision ID: d52b0b687e9e
Revises: b8ddc140328c
Create Date: 2026-09-05 16:13:10.274794

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd52b0b687e9e'
down_revision: Union[str, Sequence[str], None] = 'b8ddc140328c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Postgres allows ADD VALUE inside a transaction (PG12+) as long as the
    # new value isn't used in the same transaction — it isn't here, so this
    # is safe under Alembic's default transactional DDL.
    op.execute("ALTER TYPE recovery_action_type ADD VALUE IF NOT EXISTS 'PLACE_VOICE_CALL'")
    op.execute("ALTER TYPE communication_channel ADD VALUE IF NOT EXISTS 'VOICE'")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres has no ALTER TYPE ... DROP VALUE — removing an enum value
    # requires rebuilding the type, which isn't worth it for a demo project.
    # This migration is not meaningfully reversible.
    pass
