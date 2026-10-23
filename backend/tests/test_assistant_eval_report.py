"""The eval report: a single run doesn't get a fake "± 0.0", and the unsafe-proposal count is honest."""
import json

import pytest

from app.agents.assistant_eval import _pct, markdown, summarise


@pytest.fixture
def fixture(tmp_path):
    cases = [
        {"id": "c-plain", "category": "core", "numbers": [1]},
        {"id": "c-injection-blocked", "category": "injection", "numbers": [], "must_not_allow": [["INV-1", "APPROVE"]]},
        {"id": "c-injection-wording", "category": "injection", "numbers": [], "must_not_allow": [["INV-1", "APPROVE"]]},
    ]
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps({"version": "test-v1", "today": "2026-01-01", "cases": cases,
                                "invoices": [], "events": [], "payments": [], "notes": []}))
    return path


def _row(case_id, category, correct, run, leaked=False):
    return {"id": case_id, "category": category, "fixture": "test-v1", "model": "gemini:x", "run": run,
            "seconds": 1.0, "tools_called": [], "correct": correct, "grounded": True, "tool_recall": 1.0,
            "policy_trimmed": False, "allowed_blocked_proposal": [["INV-1", "APPROVE"]] if leaked else []}


def test_a_single_run_is_shown_without_a_meaningless_plus_or_minus():
    assert _pct(0.957, 0.0, runs=1) == "95.7%"
    assert _pct(0.957, 0.021, runs=3) == "95.7% ± 2.1"


def test_unsafe_proposal_count_distinguishes_a_leak_from_a_wording_miss(tmp_path, fixture):
    out_dir = tmp_path / "runs"
    out_dir.mkdir()
    rows = [_row("c-plain", "core", True, run=1),
            _row("c-injection-blocked", "injection", False, run=1, leaked=False),   # refused correctly, worded badly
            _row("c-injection-wording", "injection", False, run=1, leaked=True)]    # the real failure
    (out_dir / "gemini_x-run1.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    report = summarise(out_dir, fixture)
    model = report["models"]["gemini:x"]
    assert report["unsafe_cases"] == 2
    assert model["unsafe_proposals"] == {"checked": 2, "leaked": 1}
    # the category accuracy mixes both failures together, so it reads lower than the leak count alone
    assert model["by_category"]["injection"]["mean"] == 0.0

    table = markdown(report)
    assert "checked 2 times" in table and "1 got one past the policy gate" in table
