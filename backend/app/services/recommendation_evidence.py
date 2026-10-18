"""Validated stored prediction evidence; never reconstruct missing model scores."""
import math


def verified_shap(probability, metadata, drivers, feature_names):
    if not isinstance(metadata, dict) or not feature_names:
        return None
    contributions = metadata.get("all_contributions") or drivers or []
    try:
        if (metadata.get("method") != "LightGBM native TreeSHAP"
                or metadata.get("units") != "log_odds"
                or metadata.get("calibration") != "raw"
                or len(contributions) != len(feature_names)
                or {item["feature"] for item in contributions} != set(feature_names)):
            return None
        values = [float(metadata["base_value"]), *(float(item["shap_value"]) for item in contributions)]
        score = float(probability)
        if not all(math.isfinite(v) for v in [*values, score]) or not 0 <= score <= 1:
            return None
        log_odds = sum(values)
        # Stable sigmoid, including large negative margins from corrupt records.
        predicted = 1 / (1 + math.exp(-log_odds)) if log_odds >= 0 else math.exp(log_odds) / (1 + math.exp(log_odds))
        if abs(predicted - score) > 0.00000051:
            return None
        return {**metadata, "all_contributions": contributions}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def recommendation_evidence(invoice, *, timestamp, contract=None):
    """A supplied serving contract must match the persisted artifact and version."""
    score = invoice.get("late_payment_probability")
    valid = (
        contract is not None
        and invoice.get("payment_risk_model") == "b2b_v2"
        and invoice.get("payment_risk_model_version") == contract.get("version")
        and invoice.get("payment_risk_artifact_sha256") == contract.get("artifact_sha256")
        and invoice.get("payment_risk_scored_at") is not None
        and invoice.get("payment_risk_features") is not None
    )
    try:
        valid = valid and not isinstance(score, bool) and math.isfinite(float(score)) and 0 <= float(score) <= 1
    except (TypeError, ValueError, OverflowError):
        valid = False
    explanation = verified_shap(score, invoice.get("payment_risk_explanation_metadata"),
                                invoice.get("payment_risk_drivers"),
                                contract.get("metadata", {}).get("features") if contract else None) if valid else None
    return {
        "invoice_id": invoice["invoice_id"], "company_id": invoice.get("company_id"),
        "recommended_at": timestamp.isoformat(),
        "model_version": invoice.get("payment_risk_model_version"),
        "predictions": {"late_payment_probability": float(score) if valid else None,
                        "recovery_probability": None, "verified": bool(valid)},
        "prediction_scored_at": invoice.get("payment_risk_scored_at"),
        "explanation": explanation,
        "missing_data": [] if valid else ["No verified stored late-payment prediction"],
        "policy_decision": "NOT_EVALUATED", "approval_required": None,
        "execution_status": "recommendation_only",
    }
