"""Store the regional API domain Zoho's token response reports per connection.

Revision ID: f1e6a3c9d402
Revises: a9c4e71bf3d5
"""
from alembic import op
import sqlalchemy as sa

revision = "f1e6a3c9d402"
down_revision = "a9c4e71bf3d5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portal_zoho_connections", sa.Column("api_domain", sa.String(120)))


def downgrade():
    op.drop_column("portal_zoho_connections", "api_domain")
