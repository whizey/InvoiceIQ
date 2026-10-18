"""Real artifact parity, with warnings treated as failures on the new path."""
from copy import deepcopy
import warnings
import numpy as np
import pandas as pd
import pytest
from app.ml.b2b_v2_serving import load_model, load_metadata
from app.ml.named_inference import inference_input


@pytest.mark.parametrize("name", ["late_payment", "delay_over_7d", "delay_over_15d",
                                  "days_late_if_late_mean", "days_late_p80"])
def test_named_inference_preserves_exact_predictions(name):
    model = deepcopy(load_model(name))
    # Eliminate parallel reduction noise in unchanged non-LightGBM models.
    model.set_params(**{key: 1 for key in model.get_params() if key.endswith("n_jobs")})
    names = load_metadata(name)["features"]
    frame = pd.DataFrame([[float(i + 1) for i in range(len(names))],
                          [np.nan] * len(names), [0.] * len(names)], columns=names)
    method = "predict_proba" if hasattr(model, "predict_proba") else "predict"
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="X does not have valid feature names.*")
        expected = getattr(model, method)(frame)
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        estimator, prepared = inference_input(model, frame)
        actual = getattr(estimator, method)(prepared)
    np.testing.assert_array_equal(actual, expected)


def test_shuffled_input_rejected():
    model = load_model("late_payment")
    names = load_metadata("late_payment")["features"]
    frame = pd.DataFrame([[0.] * len(names)], columns=names[::-1])
    with pytest.raises(ValueError, match="feature order"):
        inference_input(model, frame)
