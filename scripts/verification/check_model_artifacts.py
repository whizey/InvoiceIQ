"""Check committed model manifests and artifact integrity without loading pickle.

Usage: python scripts/verification/check_model_artifacts.py [--verify-binaries]
The full mode requires Git LFS objects and model artifacts to be available.
"""

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "backend/app/ml/artifacts"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifacts(*, verify_binaries: bool = False, root: Path = ARTIFACTS) -> tuple[int, int]:
    if not root.exists():
        raise RuntimeError(f"Artifact directory missing: {root}")
    valid_json = 0
    checked_files = 0
    for manifest in sorted(root.rglob("*.json")):
        # The model's native XGBoost JSON is also legitimate JSON.
        data = json.loads(manifest.read_text(encoding="utf-8"))
        valid_json += 1
        if manifest.name != "runtime_contract.json":
            continue
        if not isinstance(data, dict) or not isinstance(data.get("features"), list):
            raise ValueError(f"Invalid feature contract in {manifest}")
        features = data["features"]
        if not features or len(features) != len(set(features)):
            raise ValueError(f"Empty or duplicate features in {manifest}")
        if verify_binaries:
            for file_key, hash_key in (("model_file", "model_sha256"), ("median_file", "median_sha256")):
                if file_key not in data:
                    continue
                filename = data[file_key]
                if not isinstance(filename, str) or Path(filename).name != filename:
                    raise ValueError(f"Unsafe artifact path in {manifest}: {file_key}")
                target = manifest.parent / filename
                if not target.is_file():
                    raise FileNotFoundError(target)
                expected = data.get(hash_key)
                if not isinstance(expected, str) or sha256_file(target) != expected:
                    raise ValueError(f"Artifact hash mismatch: {target}")
                checked_files += 1
    if not valid_json:
        raise ValueError("No ML JSON artifacts found")
    return valid_json, checked_files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-binaries", action="store_true")
    options = parser.parse_args()
    count, binaries = verify_artifacts(verify_binaries=options.verify_binaries)
    print(f"PASS: {count} JSON artifacts parsed; {binaries} binary/hash pairs verified")


if __name__ == "__main__":
    main()
