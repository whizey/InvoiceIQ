"""Prediction recommendations must expose only verified stored model evidence."""
from datetime import datetime, timezone
import pytest
from app.services.recommendation_evidence import verified_shap, recommendation_evidence

def meta(**updates):
    m={"method":"LightGBM native TreeSHAP","units":"log_odds","calibration":"raw",
       "base_value":0.0,"all_contributions":[{"feature":"age","shap_value":0.0}]}
    m.update(updates)
    return m

def invoice(score=0.5):
    return {"invoice_id":"invoice","company_id":"company",
            "late_payment_probability":score,"payment_risk_model":"b2b_v2",
            "payment_risk_model_version":"v1",
            "payment_risk_artifact_sha256":"digest",
            "payment_risk_scored_at":"2026-01-01T00:00:00Z",
            "payment_risk_features":{"age":1},
            "payment_risk_explanation_metadata":meta()}

CONTRACT={"version":"v1","artifact_sha256":"digest","metadata":{"features":["age"]}}
STAMP=datetime(2026,1,1,tzinfo=timezone.utc)

def test_valid_shap_matches_probability_and_is_returned():
    assert verified_shap(0.5,meta(),None,["age"])==meta()
    assert verified_shap(0.0,meta(base_value=-1000.0),None,["age"]) is not None
    result=recommendation_evidence(invoice(),timestamp=STAMP,contract=CONTRACT)
    assert result["predictions"]["verified"] is True
    assert result["explanation"]["all_contributions"][0]["feature"]=="age"
    assert result["execution_status"]=="recommendation_only"
    assert result["approval_required"] is None

@pytest.mark.parametrize("changes",[
    {"method":"approximate"},{"units":"probability"},{"calibration":"platt"},
    {"all_contributions":[]},
    {"all_contributions":[{"feature":"different","shap_value":0}]},
    {"all_contributions":[{"feature":"age","shap_value":20}]},
    {"all_contributions":[{"feature":"age","shap_value":"nan"}]},
    {"base_value":"bad"},{"base_value":float("inf")},
])
def test_shap_fails_closed_on_unverified_source(changes):
    assert verified_shap(0.5,meta(**changes),None,["age"]) is None

@pytest.mark.parametrize("score",[None,True,-0.01,1.1,float("nan"),"bad"])
def test_invalid_score_cannot_be_reported_as_verified(score):
    result=recommendation_evidence(invoice(score),timestamp=STAMP,contract=CONTRACT)
    assert result["predictions"]["verified"] is False
    assert result["predictions"]["late_payment_probability"] is None
    assert result["explanation"] is None

@pytest.mark.parametrize("field,value",[
    ("payment_risk_model","unknown"),("payment_risk_model_version","v0"),
    ("payment_risk_artifact_sha256","bad"),("payment_risk_scored_at",None),
    ("payment_risk_features",None),
])
def test_mismatched_artifact_or_missing_input_blocks_verified_recommendations(field,value):
    stored=invoice()
    stored[field]=value
    result=recommendation_evidence(stored,timestamp=STAMP,contract=CONTRACT)
    assert not result["predictions"]["verified"]
    assert result["missing_data"]

def test_no_contract_means_no_claimed_verification():
    result=recommendation_evidence(invoice(),timestamp=STAMP,contract=None)
    assert not result["predictions"]["verified"]
    assert result["policy_decision"]=="NOT_EVALUATED"
