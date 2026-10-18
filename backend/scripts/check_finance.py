"""Run the preserved-dashboard checks in the verified disposable database.

This compatibility entrypoint never creates or migrates a database. Supply
TEST_DATABASE_URL after following docs/BACKEND_TEST_RUNNER.md.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_backend_tests import main  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit("Use scripts/run_backend_tests.py for custom pytest selection")
    sys.argv = ["run_backend_tests.py", "--group", "remaining", "--match", "preserved_dashboard"]
    raise SystemExit(main())
