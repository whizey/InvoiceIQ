"""A workspace can be viewed as of a fixed date (the public 2016 ledger); NULL means today.

Revision ID: d2a7e4b1c9f6
Revises: c4f9a2d6e813
"""
from alembic import op
import sqlalchemy as sa

revision = "d2a7e4b1c9f6"
down_revision = "c4f9a2d6e813"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portal_workspaces", sa.Column("as_of", sa.Date()))


def downgrade():
    op.drop_column("portal_workspaces", "as_of")
