"""Apply only reviewed additive analytics migrations; assert financial rows unchanged.

Run from backend: .venv/bin/python scripts/apply_analytics_migrations.py --apply
No reset, seed, backdating, or external financial action. Stop local API writers first.
Only known analytics predecessor revisions are accepted; credentials are never printed.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from app.core.config import settings

TARGET = "d94e2a6b703c"
# Static table allowlist; no caller input is interpolated into SQL identifiers.
FINANCIAL_TABLES = ("companies", "contacts", "invoices", "payments", "payment_events", "recovery_cases",
    "recovery_actions", "agent_decisions", "audit_logs", "communication_logs", "policy_decisions", "promise_to_pay", "outbox_events")
INDEXES = ("ix_invoice_scope_posting", "ix_invoice_scope_prediction", "ix_case_scope_opened",
    "ix_payment_invoice_date", "ix_action_case_date", "ix_agent_case_date")


async def inspect_database():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as db:
            version = await db.scalar(text("SELECT version_num FROM alembic_version"))
            fingerprint = {}
            for table in FINANCIAL_TABLES:
                row = (await db.execute(text(f"SELECT count(*), md5(COALESCE(string_agg(md5(to_jsonb(t)::text), '' ORDER BY id::text), '')) FROM {table} t"))).one()
                fingerprint[table] = tuple(row)
            indexes = await db.scalar(text("SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND indexname = ANY(:names)"), {"names":list(INDEXES)})
            triggers = await db.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname IN ('invoiceiq_invoice_history','invoiceiq_case_history') AND tgenabled <> 'D'"))
            return version, fingerprint, indexes, triggers
    finally:
        await engine.dispose()


def main():
    before = asyncio.run(inspect_database())
    if before[0] not in {"ab72c3d49e10", "c83f1d5a602b", TARGET}:
        raise SystemExit("Refusing unrelated migration history; review the database revision first")
    if "--apply" not in sys.argv:
        print("Additive analytics migration validated. Use --apply after stopping API writers.")
        return
    if before[0] != TARGET:
        env = dict(os.environ, DATABASE_URL=settings.database_url)
        subprocess.run([sys.executable,"-m","alembic","upgrade",TARGET],env=env,check=True)
    after = asyncio.run(inspect_database())
    if before[1] != after[1]:
        raise SystemExit("Financial row fingerprints changed during migration; investigate concurrent writers")
    if after[0] != TARGET or after[2] != len(INDEXES) or after[3] != 2:
        raise SystemExit("Analytics schema verification failed")
    print(f"revision={TARGET}; analytics_indexes=6; history_triggers=2; financial_rows_unchanged=true")


if __name__ == "__main__":
    main()
