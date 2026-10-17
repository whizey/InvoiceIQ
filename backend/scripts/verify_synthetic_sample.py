"""Read-only acceptance against the dedicated disposable synthetic sample."""

import asyncio
import os

import httpx
from sqlalchemy.engine import make_url

from scripts.test_database_safety import verify


async def acceptance():
    from app.main import app
    from app.core.db import engine

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sample") as client:
        checks = (
            ("/health", 200),
            ("/health/ready", 200),
            ("/dashboard/calendar", 200),
            ("/dashboard/preserved", 200),
            ("/companies", 200),
            ("/invoices", 200),
            ("/recovery-cases", 200),
            ("/ml/models", 200),
            ("/ml/dashboard", 200),
            ("/ml/stored-predictions?start_date=2021-01-01&end_date=2029-12-31", 200),
            ("/analytics/metadata", 404),
        )
        responses = {}
        for path, expected in checks:
            response = await client.get(path)
            if response.status_code != expected:
                raise RuntimeError(f"Synthetic sample {path} returned HTTP {response.status_code}, expected {expected}")
            responses[path] = response.json()
        calendar = responses["/dashboard/calendar"]
        if calendar.get("data_mode") != "synthetic_sample" or calendar.get("llm_mode") != "deterministic_fallback":
            raise RuntimeError("Synthetic sample mode is not accurately disclosed")
        companies = responses["/companies"]
        if len(companies) != 6 or not responses["/invoices"] or not responses["/recovery-cases"]:
            raise RuntimeError("Synthetic business scenarios are incomplete")
        if responses["/ml/models"]["models"][0]["available"] is not True:
            raise RuntimeError("Packaged serving champion is unavailable")
        from app.ml.champion_serving import verified_contract
        prediction_report = responses["/ml/stored-predictions?start_date=2021-01-01&end_date=2029-12-31"]
        if prediction_report["stored_predictions"] < 6 or verified_contract()["version"] not in prediction_report["model_versions"]:
            raise RuntimeError("Synthetic sample lacks genuine versioned model scores")
        invalid_range = await client.get(
            "/ml/stored-predictions?start_date=2000-01-01&end_date=2100-01-01"
        )
        if invalid_range.status_code != 422:
            raise RuntimeError("Invalid stored-prediction date range did not return HTTP 422")
        for path in ("/invoices", "/auth/sessions", "/recovery-cases/detect-overdue"):
            response = await client.post(path, json={})
            if response.status_code != 403:
                raise RuntimeError(f"Synthetic sample write unexpectedly allowed at {path}")
        print(f"Synthetic sample acceptance passed: {len(checks)} reads, 3 denied writes, 6 companies, "
              f"{prediction_report['stored_predictions']} verified stored predictions")
    await engine.dispose()


def main():
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        raise RuntimeError("TEST_DATABASE_URL is required")
    base = make_url(raw)
    if base.database != "invoiceiq_test":
        raise RuntimeError("Expected verified primary disposable test URL")
    sample = base.set(database="invoiceiq_sample_test").render_as_string(hide_password=False)
    verify(sample)
    os.environ.update(DATABASE_URL=sample, SAMPLE_SYNTHETIC="true", PUBLIC_READ_ONLY="true",
                      SCHEDULER_ENABLED="false", OUTBOX_DISPATCH_ENABLED="false",
                      GOOGLE_API_KEY="", OPENAI_API_KEY="", GROQ_API_KEY="",
                      RESEND_API_KEY="", SARVAM_API_KEY="")
    asyncio.run(acceptance())


if __name__ == "__main__":
    main()
