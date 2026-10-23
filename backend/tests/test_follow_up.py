"""The follow-up list: baseline, fair model comparison, calibration and ranking."""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api import portal
from app.core.db import async_session_factory, engine
from app.models.portal import PortalWorkspace
from app.services import follow_up
from scripts import provision_portal_showcase as provision

TODAY = date(2026, 10, 23)


def invoice(posted_days_ago, due_in=30, total="10000", paid="0", customer="Acme", risk=None):
    posted = TODAY - timedelta(days=posted_days_ago)
    return SimpleNamespace(id=uuid.uuid4(), counterparty=customer, posting_date=posted,
                           due_date=posted + timedelta(days=due_in), amount_total=Decimal(total),
                           amount_paid=Decimal(paid), risk_probability=risk)


def test_auc_counts_ties_as_half_and_needs_both_outcomes():
    labels = [True] * 20 + [False] * 20
    assert follow_up.auc([1.0] * 20 + [0.0] * 20, labels) == 1.0
    assert follow_up.auc([0.5] * 40, labels) == 0.5
    assert follow_up.auc([0.5] * 10, [True] * 10) is None


def test_baseline_uses_only_invoices_settled_before_posting():
    first, second, third = invoice(200), invoice(150), invoice(10)
    settled = {first.id: first.due_date + timedelta(days=20), second.id: second.due_date - timedelta(days=1)}
    scores = follow_up.baseline_scores([first, second, third], settled, TODAY)
    # base rate 1/2; `third` sees one late and one on-time settlement: (1 + 0.5*2) / (2 + 2)
    assert scores[third.id] == pytest.approx(0.5)
    # `first` had no history: the workspace rate alone
    assert scores[first.id] == pytest.approx(0.5)


def test_model_is_used_only_when_it_clearly_beats_the_baseline():
    rows, settled = [], {}
    for i in range(40):
        late = i % 2 == 0
        row = invoice(120 + i, risk=0.9 if late else 0.1, customer=f"C{i}")  # model separates perfectly
        settled[row.id] = row.due_date + timedelta(days=15 if late else -2)
        rows.append(row)
    baseline = follow_up.baseline_scores(rows, settled, TODAY)  # one invoice per customer: no signal
    result = follow_up.choose_scorer(rows, settled, baseline, TODAY)
    assert result["used"] == "model" and result["model_auc"] == 1.0 and result["baseline_auc"] == 0.5
    assert "AUC 1.00 vs 0.50" in result["verdict"]
    for row in rows:
        row.risk_probability = 0.5  # now the model carries no information
    result = follow_up.choose_scorer(rows, settled, baseline, TODAY)
    assert result["used"] == "baseline" and "did not beat" in result["verdict"]


def test_calibration_reports_predicted_against_actual():
    bands = follow_up.calibration([0.7] * 10 + [0.1] * 10, [True] * 6 + [False] * 4 + [False] * 10)
    assert bands == [{"band": "0–20%", "invoices": 10, "predicted": 0.1, "actual": 0.0},
                     {"band": "60–80%", "invoices": 10, "predicted": 0.7, "actual": 0.6}]
    assert follow_up._calibration_note(bands) == "When we said 70%, 60% actually paid late (10 invoices)."


def test_priority_and_short_amounts():
    assert follow_up.priority(Decimal(100000), 0.5, 30) == 1_500_000
    assert follow_up.priority(Decimal(100000), 0.5, -3) == 0
    assert follow_up.short_rupees(140000) == "₹1.4L" and follow_up.short_rupees(45800) == "₹46k"


@pytest.mark.asyncio
async def test_follow_up_list_on_the_public_ledger(monkeypatch):
    await engine.dispose()
    workspace_id = uuid.uuid4()
    monkeypatch.setattr(provision, "SHOWCASE_ID", workspace_id)
    today = date.today()
    try:
        async with async_session_factory() as db:
            workspace = PortalWorkspace(id=workspace_id, name="Ledger", kind="showcase")
            db.add(workspace)
            await db.flush()
            await provision.seed_ledger(db, today)
            result = await portal._follow_ups(db, SimpleNamespace(token_digest="t"), workspace)
            chase = result["chase"]
            assert chase and [i["priority"] for i in chase] == sorted((i["priority"] for i in chase), reverse=True)
            assert all(i["days_overdue"] > 0 and i["late_reason"] for i in chase)
            assert all(i["late_reason"]["kind"] == "dispute" for i in result["on_hold"])
            assert all(0 <= i["days_overdue"] + 7 and i["days_overdue"] <= 0 for i in result["due_soon"])
            assert result["scorer"]["used"] in {"model", "baseline"} and result["scorer"]["verdict"]
            assert chase[0]["summary"].startswith("₹")
            await db.rollback()
    finally:
        await engine.dispose()
