"""add payment risk model version

Revision ID: 8a4e6c1d92f0
Revises: 5775ef69e805
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8a4e6c1d92f0"
down_revision: Union[str, Sequence[str], None] = (
    "5775ef69e805"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "payment_risk_model_version",
            sa.String(length=100),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "invoices",
        "payment_risk_model_version",
    )
