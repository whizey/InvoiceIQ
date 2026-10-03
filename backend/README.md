# Backend

FastAPI service for the AI Revenue Recovery Agent.

## Quickest way to run it

From the repo root: `./run.sh` — starts Postgres in Docker, sets up the venv, migrates, and launches the API with auto-reload. See the root [README](../README.md#getting-started).

## Run via Docker only (no local Python, no auto-reload)

```bash
cd ../infra
docker compose up --build
```

## Run locally, manually

Same as `run.sh` but step by step, if you want control over each part:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Then start just Postgres via Docker (`cd ../infra && docker compose up -d db`) — it's mapped to host port **5433**, not 5432, to avoid clashing with a locally-installed Postgres. `.env.example` already points at 5433.

```bash
alembic upgrade head      # apply migrations
uvicorn app.main:app --reload
```

## Database migrations (Alembic)

Models live in `app/models/`; `alembic/env.py` reads the DB URL from `app.core.config.settings` (not from `alembic.ini`), so it always matches whatever `.env`/environment is active.

```bash
alembic revision --autogenerate -m "describe the change"   # generate a migration after changing models
alembic upgrade head                                        # apply pending migrations
alembic downgrade -1                                         # roll back one migration
```

When running via Docker, migrations are applied automatically on container start (see `entrypoint.sh`) — no manual step needed.

## Seed demo data

Populates a realistic dataset covering all five recovery scenarios from the project brief (low-risk, repeat-late-payer medium-risk, high-risk escalated, promise-to-pay, and recovered-after-reminder) plus one fully healthy account. Safe to re-run — it clears prior seed data first, and all dates are relative to "today" so the scenarios (e.g. "5 days overdue") stay accurate no matter when you run it.

```bash
python -m app.seed.run
```

Via Docker: `docker compose exec api python -m app.seed.run`.

This is demo/dev data for the running app, not the (larger, purely synthetic) training set the Phase 6 ML model will use.

## Test

```bash
pytest                                    # 80 tests
pytest --cov --cov-report=term-missing    # with coverage (currently ~94%)
```

Most tests need `DATABASE_URL` pointing at a live Postgres (`docker compose up -d db`) — they're integration tests against the real ORM/DB, not mocked. Runs automatically on every push via [GitHub Actions](../.github/workflows/ci.yml).

The remaining ~6% of uncovered lines are the real-provider branches of the three auto-fallback integrations (a real OpenAI call in `app/agents/llm_client.py`, a real Redis lock in `app/core/locks.py`, a real Kafka publish in `app/events/kafka_publisher.py`) — each already verified manually against the real service (see the Phase 7/13/14 commit messages), not mocked out just to inflate a coverage number.

## Endpoints

- `GET /health` — liveness, no dependencies
- `GET /health/db` — verifies the database connection with `SELECT 1`
- `GET /companies` / `GET /companies/{id}` — company list / detail with contacts
- `GET /invoices` (filter by `status`, `company_id`; paginated) / `GET /invoices/overdue` / `GET /invoices/{id}`
- `GET /recovery-cases` — dashboard-table shape: company, invoice, amount, days overdue, risk, status, current action, recovered amount
- `GET /recovery-cases/{id}` — full case detail: invoice, actions (with the policy decision behind each), agent diagnoses/recommendations, promise-to-pay, communications, audit trail
- `GET /recovery-cases/{id}/audit-trail` — just the ordered audit log for a case
- `POST /recovery-cases/detect-overdue` — deterministic housekeeping trigger: flips newly-overdue invoices to `OVERDUE` and opens a recovery case for each, and resolves any pending promise-to-pay whose date has passed (`FULFILLED` if paid, `BROKEN` if not) — idempotent, manually/cron-triggered, no long-running consumer in V1
- `POST /recovery-cases/{id}/run` — advances a case by one full LangGraph recovery cycle (diagnosis → recommendation → policy check → execute → outcome). No-op on an already-closed/escalated-terminal case.
- `POST /invoices/{id}/simulate-payment` — mock payment simulation (stands in for a real payment webhook), so recovery can actually be demonstrated end-to-end
- `GET /dashboard/metrics` — total revenue at risk, total recovered, recovery rate, active/escalated case counts, average days overdue, breakdown by risk level

Interactive docs at `/docs` once the server is running.

## ML risk model

`app/ml/` holds the recovery-risk XGBoost model. It's trained on **synthetic data only** — see [synthetic_data.py](app/ml/synthetic_data.py) for the documented generative process. Not a production financial model.

```bash
python -m app.ml.train   # regenerates synthetic data, retrains, overwrites app/ml/artifacts/
```

The trained model (`app/ml/artifacts/recovery_risk_model.json`) and its metrics (`metrics.json`, currently AUC ~0.78) are committed to the repo, so the app runs out of the box without retraining. Real feature extraction (from actual payment history in Postgres — separate from the synthetic training generator) lives in `app/services/risk_context.py`; `POST /recovery-cases/detect-overdue` scores every case it creates automatically.

## Recovery workflow (LangGraph) + LLM setup

`app/agents/graph.py` builds the LangGraph state machine driving `POST /recovery-cases/{id}/run`; `app/services/policy_engine.py` is the deterministic gate that can override whatever the diagnosis/intervention agents recommend; `app/tools/mock_tools.py` executes whatever action the policy engine approves.

By default (no config needed) diagnosis and intervention use a deterministic rule-based fallback — the whole workflow runs and is fully testable with zero external cost or setup. To use a real LLM instead, add to `backend/.env`:

```
GOOGLE_API_KEY=...                 # from https://aistudio.google.com/apikey
LLM_MODEL=gemini-2.5-flash         # optional; this is the Gemini default
```

OpenAI still works as an alternate (`OPENAI_API_KEY`, default model `gpt-4o-mini`). If both keys are set, Gemini is used. `app/agents/llm_client.py` picks the path from config automatically, and `agent_decisions.model_name` always records which model actually ran.

Set `SCHEDULER_ENABLED=true` in `backend/.env` to run detection + one recovery cycle per active (`OPEN`/`MONITORING`) case on a timer. The HTTP endpoints remain; the scheduler calls the same services and locks. CI leaves this off.

A broken promise-to-pay (see `app/services/promise_tracking.py`) forces escalation via `app/services/policy_engine.py`'s `has_broken_promise` check — same override pattern as the high-value/overdue rule, and it also feeds into the diagnosis context so the reasoning reflects it honestly.

## Events (Kafka)

`app/events/` publishes domain events after each successful DB commit — `invoice.overdue`, `payment.received`, `recovery.case_created`, `recovery.action_completed`, `promise_to_pay.created`, `promise_to_pay.broken`, `recovery.case_closed`. No `KAFKA_BOOTSTRAP_SERVERS` set? Events are logged instead (`app/events/log_publisher.py`) — no broker required to run the app.

`docker compose up` includes Kafka (`apache/kafka:3.9.0`, KRaft mode) automatically, wired to the `api` service via `KAFKA_BOOTSTRAP_SERVERS=kafka:9092`. For local (non-Docker) dev:

```bash
cd infra && docker compose up -d kafka
```

then add to `backend/.env`:

```
KAFKA_BOOTSTRAP_SERVERS=localhost:9094   # external listener, mapped for host access
```

Watch events actually flow with the standalone demo consumer (separate from the API's request path — V1 has no long-running consumer):

```bash
python -m app.events.consumer
```

## Idempotency locks (Redis)

`app/core/locks.py` guards `POST /recovery-cases/{id}/run` (per-case) and `POST /recovery-cases/detect-overdue` (global) against concurrent triggers — the second overlapping call gets `409 Conflict` instead of racing with the first and double-executing actions. No `REDIS_URL` set? Falls back to an in-process `asyncio.Lock` — correct for a single process, but doesn't coordinate across multiple app instances.

`docker compose up` includes Redis automatically (`REDIS_URL=redis://redis:6379/0`). For local (non-Docker) dev:

```bash
cd infra && docker compose up -d redis
```

then add to `backend/.env`:

```
REDIS_URL=redis://localhost:6380/0   # external listener, mapped off the default 6379
```

## Observability

Every log line is one JSON object (`app/core/observability.py`) — `{"timestamp", "level", "logger", "message", ...}`, plus a `request_id` on any line logged during an HTTP request. `RequestContextMiddleware` generates that ID (or reuses an incoming `X-Request-ID` header), echoes it back in the response header, and binds it to a `contextvar` so every log line emitted anywhere in the call stack while handling that request — the risk engine, a LangGraph node, the Kafka publisher's failure path — carries the same ID, without threading it through every function signature. Grep any request's full story with `grep '"request_id": "<id>"'`.

Uvicorn's own plain-text access log is disabled — the middleware's structured `"request completed"` line already covers it, in a format that's actually greppable.

LangSmith tracing of the LangGraph workflow is opt-in and off by default — add to `backend/.env`:

```
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...   # from smith.langchain.com
```

No code change needed — LangChain already reports traces natively once these are set; `app/core/observability.py` just bridges our `Settings` into `os.environ` at startup, since LangChain reads the OS environment directly, not our config object.
