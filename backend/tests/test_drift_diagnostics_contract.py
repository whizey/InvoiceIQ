"""Drift diagnostics use attributable numeric source data and never retrain models."""
import gzip
import json
import sys
import pytest
from app.ml import drift_monitor as d

@pytest.mark.parametrize("reference,current,bins",[
    ([],[1],10),([1],[],10),([1,float("nan")],[1],10),
    ([1,2],[1,2],True),([1,2],[1,2],1),([1,2],[1,2],101),
])
def test_psi_invalid_inputs_are_rejected(reference,current,bins):
    with pytest.raises(ValueError):
        d.population_stability_index(reference,current,bins)

def test_psi_detects_real_population_shift():
    assert d.population_stability_index([1]*20,[1]*20)==0
    assert d.population_stability_index([1]*20,[10]*20)>0.2
    assert d.population_stability_index(list(range(100)),list(range(100)))==0

def test_numeric_csv_handles_missing_nonfinite_and_compressed(tmp_path):
    baseline=tmp_path/"baseline.csv"
    changed=tmp_path/"changed.csv"
    baseline.write_text("feature\n1\n2\nNaN\nbad\n")
    changed.write_text("feature\n3\n4\n5\n6\n")
    values,missing,rows=d.numeric_column(baseline,"feature")
    assert values==[1,2] and missing==2 and rows==4
    with pytest.raises(ValueError,match="Missing numeric"):
        d.numeric_column(baseline,"unknown")
    empty=tmp_path/"empty.csv"
    empty.write_text("feature\n")
    with pytest.raises(ValueError,match="No records"):
        d.numeric_column(empty,"feature")
    compressed=tmp_path/"changed.csv.gz"
    with gzip.open(compressed,"wt") as handle:
        handle.write(changed.read_text())
    assert d.numeric_column(compressed,"feature")[0]==[3,4,5,6]
    report=d.compare_feature(baseline,changed,"feature")
    assert report["reference_rows"]==4
    assert report["reference_missing_rate"]==0.5
    assert report["current_missing_rate"]==0
    assert report["psi_status"]=="defined"
    assert report["drift_warning"]

def test_missing_population_is_reported_without_fake_psi(tmp_path):
    a=tmp_path/"empty_numeric.csv"
    b=tmp_path/"actual.csv"
    a.write_text("feature\nbad\n")
    b.write_text("feature\n3\n")
    report=d.compare_feature(a,b,"feature")
    assert report["psi"] is None
    assert report["psi_status"]=="no_numeric_observations"
    assert report["drift_warning"]

def test_cli_can_write_json_or_print_report(tmp_path,monkeypatch,capsys):
    baseline=tmp_path/"reference.csv"
    latest=tmp_path/"current.csv"
    output=tmp_path/"reports"/"result.json"
    baseline.write_text("feature\n1\n2\n3\n")
    latest.write_text("feature\n1\n3\n9\n")
    args=["drift","--reference",str(baseline),"--current",str(latest),
          "--features","feature"]
    monkeypatch.setattr(sys,"argv",args+["--output",str(output)])
    d.main()
    assert json.loads(output.read_text())["features"][0]["feature"]=="feature"
    assert "saved" in capsys.readouterr().out
    monkeypatch.setattr(sys,"argv",args)
    d.main()
    assert json.loads(capsys.readouterr().out)["features"][0]["feature"]=="feature"
