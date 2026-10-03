import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.db import engine
from app.main import app
from app.seed.run import seed

NIL_UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(scope="module")
def client():
    asyncio.run(seed())
    # The engine's pooled connections are bound to the event loop that just
    # closed with asyncio.run(); dispose so the TestClient's requests (on
    # its own portal loop) open fresh connections instead of reusing dead ones.
    asyncio.run(engine.dispose())

    # Used as a context manager so all requests share one background event
    # loop (a "portal") for the lifetime of the fixture — without this,
    # every individual .get() call gets its own loop, which the async
    # SQLAlchemy engine's connection pool can't survive across calls.
    with TestClient(app) as test_client:
        yield test_client

    # Likewise, dispose after this module's portal loop closes, so later
    # test modules (their own event loop) don't inherit dead connections.
    asyncio.run(engine.dispose())


def test_list_companies(client):
    resp = client.get("/companies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 6
    assert {c["name"] for c in data} >= {"Vertex Infra Solutions", "Northwind Traders Pvt Ltd"}


def test_get_company_detail_includes_contacts(client):
    company_id = client.get("/companies").json()[0]["id"]
    resp = client.get(f"/companies/{company_id}")
    assert resp.status_code == 200
    assert len(resp.json()["contacts"]) >= 1


def test_get_company_404(client):
    resp = client.get(f"/companies/{NIL_UUID}")
    assert resp.status_code == 404


def test_list_overdue_invoices(client):
    resp = client.get("/invoices/overdue")
    assert resp.status_code == 200
    data = resp.json()
    numbers = {inv["invoice_number"] for inv in data}
    assert numbers == {
        "INV-NORTHWIND-2001",
        "INV-BLUEPEAK-2005",
        "INV-VERTEX-3010",
        "INV-SUNDIAL-4002",
    }
    for inv in data:
        assert inv["status"] == "OVERDUE"
        assert inv["amount_total"] != inv["amount_paid"]


def test_invoice_overdue_route_not_shadowed_by_id_route(client):
    # /invoices/overdue must resolve to the overdue-list route, not a 422
    # from trying to parse "overdue" as a UUID path param.
    resp = client.get("/invoices/overdue")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_invoice_404(client):
    resp = client.get(f"/invoices/{NIL_UUID}")
    assert resp.status_code == 404


def test_get_invoice_by_id_matches_list_entry(client):
    overdue = client.get("/invoices/overdue").json()
    target = overdue[0]

    resp = client.get(f"/invoices/{target['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["invoice_number"] == target["invoice_number"]
    assert body["company"]["name"] == target["company"]["name"]


def test_list_invoices_filters_by_status(client):
    resp = client.get("/invoices", params={"status": "PAID"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert all(inv["status"] == "PAID" for inv in data)


def test_list_invoices_filters_by_company_id(client):
    overdue = client.get("/invoices/overdue").json()
    company_id = overdue[0]["company"]["id"]

    resp = client.get("/invoices", params={"company_id": company_id})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert all(inv["company"]["id"] == company_id for inv in data)


def test_list_invoices_pagination(client):
    page1 = client.get("/invoices", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/invoices", params={"limit": 2, "offset": 2}).json()
    assert len(page1) == 2
    assert {inv["id"] for inv in page1}.isdisjoint({inv["id"] for inv in page2})


def test_health_db_endpoint(client):
    resp = client.get("/health/db")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_recovery_cases_matches_dashboard_shape(client):
    resp = client.get("/recovery-cases")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 5

    by_invoice = {c["invoice_number"]: c for c in data}
    assert by_invoice["INV-VERTEX-3010"]["status"] == "ESCALATED"
    assert by_invoice["INV-VERTEX-3010"]["risk_level"] == "HIGH"
    assert by_invoice["INV-VERTEX-3010"]["current_action"] == "ESCALATE"
    assert by_invoice["INV-VERTEX-3010"]["days_overdue"] == 60

    assert by_invoice["INV-AARAV-1004"]["status"] == "CLOSED"
    assert by_invoice["INV-AARAV-1004"]["recovered_amount"] == "95000.00"

    assert by_invoice["INV-SUNDIAL-4002"]["current_action"] == "TRACK_PROMISE_TO_PAY"


def test_recovery_case_detail_full_narrative(client):
    cases = client.get("/recovery-cases").json()
    vertex_id = next(c["id"] for c in cases if c["invoice_number"] == "INV-VERTEX-3010")

    resp = client.get(f"/recovery-cases/{vertex_id}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "ESCALATED"
    assert body["invoice"]["company"]["name"] == "Vertex Infra Solutions"
    assert len(body["actions"]) == 3
    assert body["actions"][-1]["action_type"] == "ESCALATE"
    assert body["actions"][-1]["policy_decisions"][0]["decision"] == "APPROVED"
    assert len(body["agent_decisions"]) == 2
    assert len(body["audit_logs"]) == 8
    assert body["audit_logs"] == sorted(body["audit_logs"], key=lambda a: a["occurred_at"])


def test_recovery_case_audit_trail_matches_detail(client):
    cases = client.get("/recovery-cases").json()
    case_id = cases[0]["id"]

    detail = client.get(f"/recovery-cases/{case_id}").json()
    trail = client.get(f"/recovery-cases/{case_id}/audit-trail").json()

    assert len(trail) == len(detail["audit_logs"])
    assert [a["id"] for a in trail] == [a["id"] for a in detail["audit_logs"]]


def test_promise_to_pay_case_has_pending_promise(client):
    cases = client.get("/recovery-cases").json()
    sundial_id = next(c["id"] for c in cases if c["invoice_number"] == "INV-SUNDIAL-4002")

    detail = client.get(f"/recovery-cases/{sundial_id}").json()
    assert len(detail["promises_to_pay"]) == 1
    assert detail["promises_to_pay"][0]["status"] == "PENDING"
    assert detail["promises_to_pay"][0]["promised_amount"] == "320000.00"


def test_recovery_case_404(client):
    resp = client.get(f"/recovery-cases/{NIL_UUID}")
    assert resp.status_code == 404

    resp = client.get(f"/recovery-cases/{NIL_UUID}/audit-trail")
    assert resp.status_code == 404


def test_dashboard_metrics_endpoint(client):
    resp = client.get("/dashboard/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(data["total_revenue_at_risk"]) == Decimal("2780000.00")
    assert Decimal(data["total_revenue_recovered"]) == Decimal("95000.00")
    assert data["active_cases"] == 4
    assert data["escalated_cases"] == 1


def test_dashboard_policy_overrides_endpoint(client):
    resp = client.get("/dashboard/policy-overrides")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_evaluated"] == 9
    assert data["override_count"] == 1
    assert data["override_rate"] == pytest.approx(1 / 9)
    assert data["by_rule"]["high_value_overdue_forced_escalate"] == 1
    assert len(data["examples"]) == 1
    assert data["examples"][0]["company_name"] == "Vertex Infra Solutions"
    assert data["examples"][0]["recommended_action_type"] == "SEND_PAYMENT_LINK"
    assert data["examples"][0]["action_type"] == "ESCALATE"


def test_recovery_action_out_includes_recommended_action_type(client):
    # Vertex's third action is the flagship override: recommended a
    # reminder, policy forced escalation instead.
    cases = client.get("/recovery-cases").json()
    vertex_id = next(c["id"] for c in cases if c["invoice_number"] == "INV-VERTEX-3010")
    detail = client.get(f"/recovery-cases/{vertex_id}").json()

    third_action = next(a for a in detail["actions"] if a["sequence_number"] == 3)
    assert third_action["recommended_action_type"] == "SEND_PAYMENT_LINK"
    assert third_action["action_type"] == "ESCALATE"
    assert third_action["policy_decisions"][0]["rule"] == "high_value_overdue_forced_escalate"


def test_detect_overdue_endpoint_is_idempotent_against_seeded_data(client):
    # The seeded scenarios are already fully created (Phase 3 hand-seeds
    # cases directly), so the engine should find nothing new to do.
    resp = client.post("/recovery-cases/detect-overdue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["invoices_marked_overdue"] == 0
    assert body["cases_created"] == 0
    assert body["case_ids"] == []
    # Sundial's seeded promise isn't due yet, so nothing should resolve either.
    assert body["promises_fulfilled"] == 0
    assert body["promises_broken"] == 0



def test_risk_explanation_endpoint(client):
    cases = client.get("/recovery-cases").json()
    case_id = cases[0]["id"]

    resp = client.get(
        f"/recovery-cases/{case_id}/risk-explanation"
    )

    assert resp.status_code == 200

    body = resp.json()

    assert 0 <= body["recovery_probability"] <= 1
    assert 0 <= body["risk_score"] <= 100
    assert body["risk_level"] in {"LOW", "MEDIUM", "HIGH"}

    contributions = body["contributions"]

    assert len(contributions) == 9
    assert all(item["label"] for item in contributions)
    assert all("_" not in item["label"] for item in contributions)

    magnitudes = [
        abs(item["shap_value"])
        for item in contributions
    ]

    assert magnitudes == sorted(
        magnitudes,
        reverse=True,
    )


def test_risk_explanation_404_for_missing_case(client):
    resp = client.get(
        f"/recovery-cases/{NIL_UUID}/risk-explanation"
    )

    assert resp.status_code == 404
