import json
from pathlib import Path


ARTIFACT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "ml"
    / "artifacts"
    / "b2b_v2"
)


def read_json(relative_path: str):
    return json.loads(
        (ARTIFACT_ROOT / relative_path).read_text()
    )


def test_b2b_v2_uses_one_conservative_clearance_quantile():
    champions = read_json("champion_manifest.json")
    calibration = read_json("calibration/quantile_calibration.json")
    serving = read_json("serving_contract.json")
    agent = read_json("agent_answer_contract.json")

    assert "days_late_p50" not in champions
    assert set(calibration) == {"p80"}
    assert not (ARTIFACT_ROOT / "models" / "days_late_p50").exists()

    outputs = serving["model_outputs"]
    assert "conservative_clearance_window_days" in outputs
    assert "conditional_p50_days_late" not in outputs

    late_answers = agent["supported_questions"]["if_late_how_many_days"]["answers"]
    assert late_answers == [
        "conditional_expected_days_late",
        "conservative_clearance_window_days",
    ]
