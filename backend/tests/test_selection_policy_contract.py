"""A model cannot be selected using test outcomes or mismatched features."""
import json
import pytest
from app.ml import selection_policy as s

def report(name, auc):
    return {"model":name,"target":s.TARGET_COLUMN,"test_used":False,
            "features":{"order":s.FEATURES},
            "validation":{"within_period_auc":auc, "pooled_roc_auc":auc,
                          "pooled_pr_auc":0.5,"log_loss":0.4,"brier":0.2},
            "temporal_folds":{"mean":auc,"min":auc-0.1,"std":0.01,
                              "inverted_folds":[],"stable":True}}

@pytest.mark.parametrize("mutation", [
    {"model":"wrong"},{"target":"untrusted"},{"test_used":True},
    {"features":{"order":["invalid"]}},
    {"validation":{}},{"validation":{"within_period_auc":"not-numeric"}},
    {"validation":None},
])
def test_report_validation_rejects_incompatible_evidence(mutation):
    row=report("xgboost",0.75)
    row.update(mutation)
    with pytest.raises(s.ModelSelectionError):
        s.validate_report("xgboost",row)

def test_report_selection_is_validation_only():
    rows={name:report(name,auc) for name,auc in zip(s.FINAL_MODEL_FAMILIES,[0.71,0.84,0.76])}
    selected=s.select_final_model(rows)
    assert selected.model_name=="random_forest"
    assert selected.primary_metric=="within_period_auc"
    assert selected.primary_value==pytest.approx(0.84)
    assert selected.diagnostics["pooled_roc_auc"]==0.84
    assert selected.diagnostics["stable"] is True
    assert selected.diagnostics["temporal_min"]==pytest.approx(0.74)

def test_missing_family_fails_closed():
    with pytest.raises(s.ModelSelectionError,match="Missing final model"):
        s.select_final_model({"xgboost":report("xgboost",0.8)})

def test_invalid_member_blocks_selection():
    rows={name:report(name,0.8) for name in s.FINAL_MODEL_FAMILIES}
    rows["gradient_boosting"]["test_used"]=True
    with pytest.raises(s.ModelSelectionError):
        s.select_final_model(rows)

def test_report_loading_and_main_prints_selected_model(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(s,"ARTIFACT_ROOT",tmp_path)
    rows={name:report(name,auc) for name,auc in zip(s.FINAL_MODEL_FAMILIES,[0.61,0.77,0.68])}
    for name,row in rows.items():
        directory=tmp_path/name
        directory.mkdir()
        (directory/"report.json").write_text(json.dumps(row))
    assert s.load_final_reports()==rows
    s.main()
    assert "random_forest" in capsys.readouterr().out
    (tmp_path/"random_forest"/"report.json").unlink()
    assert "random_forest" not in s.load_final_reports()
