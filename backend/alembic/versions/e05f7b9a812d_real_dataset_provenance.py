"""Persist public dataset and runtime prediction provenance without changing source artifacts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "e05f7b9a812d"
down_revision = "d94e2a6b703c"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE company_segment ADD VALUE IF NOT EXISTS 'UNKNOWN'")
    op.add_column("invoices", sa.Column("payment_risk_artifact_sha256", sa.String(64)))
    op.add_column("invoices", sa.Column("payment_risk_explanation_metadata", JSONB()))
    op.create_table("dataset_imports",
        sa.Column("source", sa.String(100), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_as_of", sa.Date(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("customer_count", sa.Integer(), nullable=False),
        sa.Column("amount_total", sa.Numeric(20, 2), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("offline_evaluations",
        sa.Column("source", sa.String(250), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("model_family", sa.String(100), nullable=False),
        sa.Column("evaluation_context", sa.String(100), nullable=False),
        sa.Column("metrics", JSONB(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False))
    # The application can read frozen metrics but cannot revise evaluation results.
    op.execute("""CREATE FUNCTION reject_offline_evaluation_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Offline evaluations are immutable'; END;
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER offline_evaluations_immutable BEFORE UPDATE OR DELETE
        ON offline_evaluations FOR EACH ROW EXECUTE FUNCTION reject_offline_evaluation_mutation()""")


def downgrade():
    # Keep the enum label: removing it would require rewriting source-derived
    # unknown segments into invented business classifications.
    op.drop_table("offline_evaluations")
    op.execute("DROP FUNCTION reject_offline_evaluation_mutation()")
    op.drop_table("dataset_imports")
    op.drop_column("invoices", "payment_risk_explanation_metadata")
    op.drop_column("invoices", "payment_risk_artifact_sha256")
