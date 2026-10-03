from app.models import Base

EXPECTED_TABLES = {
    "companies",
    "contacts",
    "invoices",
    "payments",
    "payment_events",
    "recovery_cases",
    "recovery_actions",
    "agent_decisions",
    "promise_to_pay",
    "communication_logs",
    "policy_decisions",
    "audit_logs",
}


def test_all_domain_tables_registered():
    assert EXPECTED_TABLES.issubset(Base.metadata.tables.keys())


def test_money_columns_use_numeric():
    invoices = Base.metadata.tables["invoices"]
    assert str(invoices.c.amount_total.type) == "NUMERIC(14, 2)"
    assert str(invoices.c.amount_paid.type) == "NUMERIC(14, 2)"

    recovery_cases = Base.metadata.tables["recovery_cases"]
    assert str(recovery_cases.c.revenue_at_risk.type) == "NUMERIC(14, 2)"
    assert str(recovery_cases.c.recovered_amount.type) == "NUMERIC(14, 2)"
