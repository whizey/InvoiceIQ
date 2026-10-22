"""Store an embedding per invoice note for hybrid (keyword + semantic) note search.

Revision ID: b3e8c1d5f702
Revises: a7d3f5c9e214
"""
from alembic import op
import sqlalchemy as sa

revision = "b3e8c1d5f702"
down_revision = "a7d3f5c9e214"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("portal_notes", sa.Column("embedding", sa.LargeBinary()))
    op.add_column("portal_notes", sa.Column("embedding_model", sa.String(64)))


def downgrade():
    op.drop_column("portal_notes", "embedding_model")
    op.drop_column("portal_notes", "embedding")
