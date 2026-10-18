"""Verify disposable targets, then run isolated PostgreSQL test partitions."""

import argparse
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import asyncpg
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from scripts.test_database_safety import verify

BACKEND = Path(__file__).resolve().parents[1]
TARGETS = {
    "TEST_DATABASE_URL": "invoiceiq_test",
    "PHASE1_TEST_DATABASE_URL": "invoiceiq_phase1_corpus_test",
    "PHASE1_REPLAY_DATABASE_URL": "invoiceiq_phase1_replay_test",
    "INVOICEIQ_PHASE7_DB_URL": "invoiceiq_test",
}
PHASE5 = "invoiceiq_phase1_regression_test"
CORPUS = BACKEND.parent / "data/raw/b2b_primary/b2b_invoice_payment_45839.csv"
CORPUS_SHA256 = "2a7f1dfc4e52231d4a4cb60ee019d6cef3730708afffb1c12786b4d3061bb32d"


async def migration_version(raw):
    url = make_url(raw)
    conn = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                                 password=url.password, database=url.database, timeout=10)
    try:
        async with conn.transaction(readonly=True):
            version = await conn.fetchval("SELECT version_num FROM alembic_version")
            invoices = await conn.fetchval("SELECT count(*) FROM invoices")
            cases = await conn.fetchval("SELECT count(*) FROM recovery_cases")
            return version, invoices, cases
    finally:
        await conn.close()


def preflight():
    if not CORPUS.is_file() or hashlib.sha256(CORPUS.read_bytes()).hexdigest() != CORPUS_SHA256:
        raise RuntimeError("The exact public test corpus is missing or has changed")
    missing = [key for key in TARGETS if not os.environ.get(key)]
    if missing:
        raise RuntimeError("Missing test database variables: " + ", ".join(missing))
    primary = make_url(os.environ["TEST_DATABASE_URL"])
    urls = {name: os.environ[name] for name in TARGETS}
    urls["PHASE5_DATABASE_URL"] = primary.set(database=PHASE5).render_as_string(hide_password=False)
    for key, raw in urls.items():
        expected = PHASE5 if key == "PHASE5_DATABASE_URL" else TARGETS[key]
        if make_url(raw).database != expected:
            raise RuntimeError(f"{key} must target {expected}")
    if len({make_url(raw).database for raw in urls.values()}) != 4:
        raise RuntimeError("The primary, Phase 5, corpus, and replay databases must be separate")
    head = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_current_head()
    for key, raw in urls.items():
        verify(raw)
        version, invoices, cases = asyncio.run(migration_version(raw))
        if version != head:
            raise RuntimeError(f"{key} is not migrated to Alembic head")
        if key == "PHASE1_TEST_DATABASE_URL" and invoices != 45839:
            raise RuntimeError("Corpus database does not contain the 45,839 invoices")
        if key == "PHASE1_REPLAY_DATABASE_URL" and cases != 3:
            raise RuntimeError("Replay database does not contain the three historical cases")
        print(f"Verified migrated {key}: {make_url(raw).database}", flush=True)
    return urls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()
    try:
        urls = preflight()
    except Exception as exc:
        print(f"BLOCKED before pytest ({type(exc).__name__}); check the private test environment, "
              "disposable container, corpus, and migrations.", file=sys.stderr)
        return 1
    results = []
    for name, database, files in (
        ("phase5", PHASE5, ["tests/test_phase5_postgres_company_isolation.py"]),
        ("remaining", "invoiceiq_test", ["tests", "--ignore=tests/test_phase5_postgres_company_isolation.py"]),
    ):
        raw = urls["PHASE5_DATABASE_URL"] if name == "phase5" else urls["TEST_DATABASE_URL"]
        env = dict(os.environ, TEST_DATABASE_URL=raw, DATABASE_URL=raw,
                   PHASE1_SOURCE_DATABASE_URL=urls["PHASE1_TEST_DATABASE_URL"])
        command = [sys.executable, "-m", "pytest", *files, "-q", "--tb=short"]
        if args.coverage and name == "remaining":
            command += ["--cov", "--cov-report=term-missing", "--cov-fail-under=80"]
        print(f"Running {name} on {database}", flush=True)
        results.append(subprocess.run(command, cwd=BACKEND, env=env).returncode)
    return int(any(results))


if __name__ == "__main__":
    sys.exit(main())
