import json
from pathlib import Path


ROOT = Path(
    "app/ml/artifacts/bpi_survival"
)


def test_bpi_contract():
    contract = json.loads(
        (
            ROOT
            / "serving_contract.json"
        ).read_text()
    )

    assert (
        contract["champion"]
        == "lightgbm_hazard"
    )

    assert (
        contract["serving_horizons_days"]
        == [7, 30, 60, 90]
    )

    assert (
        contract["default_outputs"]
        == [
            "clearance_probability_7d",
            "clearance_probability_30d",
            "clearance_probability_60d",
            "clearance_probability_90d",
            "likely_clearance_window_days",
        ]
    )

    serialized = json.dumps(
        contract
    ).lower()

    assert "median_clearance_window" not in serialized
    assert "p50" not in serialized
    assert "p80" not in serialized


def test_old_bpi_experiment_removed():
    assert not Path(
        "app/ml/challenge_bpi_survival_models.py"
    ).exists()

    assert not (
        ROOT
        / "survival_model_tournament"
    ).exists()
