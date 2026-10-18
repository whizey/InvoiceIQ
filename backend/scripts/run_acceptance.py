"""Compatibility entrypoint for the guarded, pre-migrated test runner."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_backend_tests import main  # noqa: E402

MODULES = (
    "end_to_end_intelligence", "date_analytics", "champion_serving",
    "outbox", "kafka_lifecycle", "review_gate", "review_revalidation",
    "recovery_workflow", "policy_engine", "external_event_services",
    "external_llm_acceptance", "analytics_history",
)

if __name__ == "__main__":
    requested = set(sys.argv[1:])
    if requested - {"--full", "--external-llm"}:
        raise SystemExit("Supported options: --full, --external-llm")
    if "--external-llm" in requested:
        os.environ["TEST_EXTERNAL_LLM"] = "1"
    options = [] if "--full" in requested else ["--group", "remaining", "--match", " or ".join(MODULES)]
    sys.argv = ["run_backend_tests.py", *options]
    raise SystemExit(main())
