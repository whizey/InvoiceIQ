"""communication log recipient email

Revision ID: b8ddc140328c
Revises: c8e9e66647de
Create Date: 2026-09-05 15:57:11.562052

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8ddc140328c'
down_revision: Union[str, Sequence[str], None] = 'c8e9e66647de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "communication_logs",
        sa.Column("recipient_email", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("communication_logs", "recipient_email")
