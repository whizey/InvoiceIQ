"""Add dated payment ledger to maintain historically accurate cash collections.

Revision ID: a02849ab1830
Revises: 6b4e8d2f091a
"""
from alembic import op
import sqlalchemy as sa

revision = "a02849ab1830"
down_revision = "6b4e8d2f091a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portal_payments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("portal_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invoice_id", sa.UUID(), sa.ForeignKey("portal_invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.CheckConstraint("amount > 0", name="portal_payment_positive"),
    )
    op.create_index("ix_portal_payment_scoped_date", "portal_payments", ["workspace_id", "paid_on"])


def downgrade():
    op.drop_index("ix_portal_payment_scoped_date", table_name="portal_payments")
    op.drop_table("portal_payments")
