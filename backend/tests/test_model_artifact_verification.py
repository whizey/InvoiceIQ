"""Provenance integrity checks do not deserialize potentially unsafe joblib models."""

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/verification/check_model_artifacts.py"
SPEC = importlib.util.spec_from_file_location("artifact_checker", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_artifact_metadata_is_valid_in_repository():
    files, _ = MODULE.verify_artifacts()
    assert files > 0


def test_hash_check_fails_on_modified_binary(tmp_path):
    location = tmp_path / "model"
    location.mkdir()
    (location / "model.joblib").write_bytes(b"altered")
    (location / "runtime_contract.json").write_text(json.dumps({
        "features": ["amount"],
        "model_file": "model.joblib",
        "model_sha256": "0" * 64,
    }))
    try:
        MODULE.verify_artifacts(verify_binaries=True, root=tmp_path)
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("Corrupt model must fail verification")
