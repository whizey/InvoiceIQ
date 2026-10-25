"""Store Zoho's own invoice id and, per payment allocation, a Zoho payment id -- real payment records
instead of only inferring a payment from an invoice's balance dropping.

Revision ID: a3e5f82c9d16
Revises: d8f3c6a1b472
"""
from alembic import op
import sqlalchemy as sa

revision = "a3e5f82c9d16"
down_revision = "d8f3c6a1b472"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portal_invoices", sa.Column("zoho_invoice_id", sa.String(64)))
    op.add_column("portal_payments", sa.Column("zoho_payment_id", sa.String(64)))
    op.create_unique_constraint(
        "uq_portal_payment_zoho_id", "portal_payments", ["workspace_id", "zoho_payment_id", "invoice_id"])


def downgrade():
    op.drop_constraint("uq_portal_payment_zoho_id", "portal_payments", type_="unique")
    op.drop_column("portal_payments", "zoho_payment_id")
    op.drop_column("portal_invoices", "zoho_invoice_id")
