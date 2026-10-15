"""Run isolated PostgreSQL test partitions in fresh Python processes."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from scripts.test_database_safety import validate_url, verify  # noqa: E402
PHASE5 = "tests/test_phase5_postgres_company_isolation.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=("all", "phase5", "remaining"), default="all")
    parser.add_argument("--match", help="Optional pytest -k expression for targeted verification")
    parser.add_argument("--coverage", action="store_true", help="Gate coverage in the remaining partition")
    args = parser.parse_args()
    base = os.environ.get("TEST_DATABASE_URL", "")
    if not base:
        parser.error("Set TEST_DATABASE_URL to the disposable invoiceiq_test connection")
    url = validate_url(base)
    groups = [("phase5", "invoiceiq_phase1_regression_test"), ("remaining", "invoiceiq_test")]
    results = []
    for group, database in groups:
        if args.group not in ("all", group):
            continue
        raw = url.set(database=database).render_as_string(hide_password=False)
        try:
            verify(raw)
        except Exception as exc:
            print(f"{group}: BLOCKED before pytest ({type(exc).__name__}: database verification failed)", flush=True)
            results.append(1)
            continue
        env = dict(os.environ, TEST_DATABASE_URL=raw, DATABASE_URL=raw,
                   INVOICEIQ_PHASE7_DB_URL=raw)
        # Optional corpus/replay suites require their own prepared data and are
        # skipped by their existing fixtures. Never inherit private source URLs.
        for key in ("PHASE1_TEST_DATABASE_URL", "PHASE1_SOURCE_DATABASE_URL", "PYTEST_ADDOPTS"):
            env.pop(key, None)
        command = [sys.executable, "-m", "pytest", "-q", "-ra"]
        command += [PHASE5] if group == "phase5" else ["tests", f"--ignore={PHASE5}"]
        if args.coverage and group == "remaining":
            command += ["--cov", "--cov-report=term-missing", "--cov-fail-under=80"]
        if args.match:
            command += ["-k", args.match]
        print(f"Running {group} on verified {database}", flush=True)
        started = perf_counter()
        code = subprocess.run(command, cwd=BACKEND, env=env).returncode
        print(f"{group}: pytest exit {code}, elapsed {perf_counter() - started:.2f}s", flush=True)
        results.append(code)
    return 1 if any(results) else 0


if __name__ == "__main__":
    sys.exit(main())
