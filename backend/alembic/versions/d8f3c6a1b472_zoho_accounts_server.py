"""Store the accounts-server a Zoho connection's code was issued from, for refreshing its token later.

Revision ID: d8f3c6a1b472
Revises: c7b4d9e2a815
"""
from alembic import op
import sqlalchemy as sa

revision = "d8f3c6a1b472"
down_revision = "c7b4d9e2a815"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portal_zoho_connections", sa.Column("accounts_server", sa.String(120)))


def downgrade():
    op.drop_column("portal_zoho_connections", "accounts_server")
