"""Preserve fitted preprocessing and LightGBM's positional feature names."""
import pandas as pd


def inference_input(pipeline, frame):
    if not hasattr(pipeline, "steps"):
        return pipeline, frame
    estimator = pipeline.steps[-1][1]
    if type(estimator).__name__ not in {"LGBMClassifier", "LGBMRegressor"}:
        return pipeline, frame
    expected = list(pipeline.feature_names_in_)
    if list(frame.columns) != expected:
        raise ValueError("Inference feature order differs from fitted pipeline")
    preprocessing = pipeline[:-1]
    transformed_names = list(preprocessing.get_feature_names_out())
    if transformed_names != expected:
        raise ValueError("Preprocessing changed the feature order")
    fitted_names = list(estimator.feature_name_)
    positional_names = [f"Column_{i}" for i in range(len(expected))]
    if fitted_names not in (expected, positional_names):
        raise ValueError("LightGBM feature order differs from fitted preprocessing")
    # Training used an array, so LightGBM assigned Column_N names. Attach those
    # same names to the unchanged imputed values, not the raw input features.
    values = preprocessing.transform(frame)
    return estimator, pd.DataFrame(values, columns=fitted_names, index=frame.index)
