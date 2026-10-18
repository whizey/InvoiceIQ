"""Read existing BPI evaluations without mixing survival with payment-risk inference."""
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.models import OfflineEvaluation

ARTIFACTS = Path(__file__).resolve().parents[1] / "ml" / "artifacts"


def bpi_evaluations():
    temporal = ARTIFACTS / "bpi_survival/temporal_backtest.json"
    landmarks = ARTIFACTS / "stage3_validation/bpi/fold_0/landmark_metrics.csv"
    for path, context in ((temporal, "offline_temporal_validation"), (landmarks, "offline_temporal_fold_0_landmarks")):
        if not path.is_file():
            raise FileNotFoundError(f"Required BPI evaluation artifact unavailable: {path.relative_to(ARTIFACTS)}")
        contents = path.read_bytes()
        if path.suffix == ".json":
            metrics = json.loads(contents)
        else:
            # Preserve source precision; numbers are parsed by consumers for display only.
            metrics = {"rows": list(csv.DictReader(contents.decode().splitlines()))}
        yield {"source": str(path.relative_to(ARTIFACTS)),
               "source_sha256": hashlib.sha256(contents).hexdigest(),
               "model_family": "bpi_process_survival", "evaluation_context": context,
               "metrics": metrics}


async def import_bpi_evaluations(session):
    for record in bpi_evaluations():
        existing = await session.get(OfflineEvaluation, record["source"])
        if existing is not None:
            if existing.source_sha256 != record["source_sha256"] or existing.metrics != record["metrics"]:
                raise ValueError("Frozen BPI evaluation differs from its imported provenance")
        else:
            session.add(OfflineEvaluation(**record, imported_at=datetime.now(timezone.utc)))
