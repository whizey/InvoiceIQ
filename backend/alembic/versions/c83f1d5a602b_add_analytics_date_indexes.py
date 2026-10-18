"""Scoped operational date indexes; no business data changes."""
from alembic import op
import sqlalchemy as sa
revision = "c83f1d5a602b"
down_revision = "ab72c3d49e10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_invoice_scope_posting", "invoices", ["company_id", "currency", sa.text("COALESCE(posting_date, issue_date)")])
    op.create_index("ix_invoice_scope_prediction", "invoices", ["company_id", "payment_risk_scored_at"])
    op.create_index("ix_case_scope_opened", "recovery_cases", ["company_id", "opened_at"])
    op.create_index("ix_payment_invoice_date", "payments", ["invoice_id", "payment_date"])
    op.create_index("ix_action_case_date", "recovery_actions", ["recovery_case_id", "created_at"])
    op.create_index("ix_agent_case_date", "agent_decisions", ["recovery_case_id", "created_at"])


def downgrade():
    for name, table in [("ix_agent_case_date","agent_decisions"),("ix_action_case_date","recovery_actions"),
                        ("ix_payment_invoice_date","payments"),("ix_case_scope_opened","recovery_cases"),
                        ("ix_invoice_scope_prediction","invoices"),("ix_invoice_scope_posting","invoices")]:
        op.drop_index(name, table_name=table)
