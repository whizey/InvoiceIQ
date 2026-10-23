# InvoiceIQ backend

FastAPI service for invoices, payment risk, recovery cases, approvals,
reconciliation, audits, analytics and background processing. Start with the
[root README](../README.md) for architecture and current release status.

## Local development

Use Python 3.12+ and PostgreSQL 16. From the repository root, `./run.sh`
starts the local development database and both application servers. For manual
backend setup:

```sh
cd backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
# Start the existing local development PostgreSQL separately.
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
```

The sample local URL uses `127.0.0.1:5435/recovery_db`. Migration and seed
commands write to the configured database. Check `DATABASE_URL` before running
them. Never seed or run integration fixtures against valuable data.

## Isolated tests

Use [the guarded test setup](docs/BACKEND_TEST_RUNNER.md). It verifies a
separate, explicitly disposable PostgreSQL container on `127.0.0.1:55436`
before destructive fixtures. Phase 5 and the remaining suite run in different
databases and Python processes. Running plain `pytest` without the prepared
container fails closed on database access. Optional private replay and
Kafka/Redis lifecycle tests have separate fixture requirements; skips are not
passes. See [docs/TESTING.md](../docs/TESTING.md) for what's actually run.

## Runtime boundaries

- `GET /health` is liveness; `GET /health/db` checks PostgreSQL;
  `GET /health/ready` checks required runtime dependencies.
- Invoice, payment and company routes expose financial records with
  company-scoped authorization in hosted environments.
- Recovery-case routes implement detection, risk scoring, the policy-gated
  LangGraph cycle, reviewer decisions, reconciliation and audit trails.
  The payment simulation endpoint is a stand-in, not a bank
  integration.
- `/dashboard/preserved` and `/dashboard/workspace/*` serve the current
  Next.js dashboard. The web server calls them through server-side proxy
  routes with a workspace credential kept in an HTTP-only cookie.
- `/ml/*` serves the evaluated champion artifacts and versioned inference
  contracts. The artifacts are not retrained during startup.
- Alembic migrations run in `entrypoint.sh` when the API container starts.
  `SCHEDULER_ENABLED` controls the recovery scheduler; transactional outbox
  dispatch can run without Kafka using its log publisher. Redis coordinates
  locks in multi-process deployments.

Hosted writes require an operator credential. Analytics and reviewer access
require separately configured tokens and explicit company scopes. Do not put
operator credentials in public browser configuration. `PUBLIC_READ_ONLY`
is a separate mode and blocks mutations. CORS is not broadly enabled because
the dashboard proxies API requests from the Next.js server.

## Optional AI integrations

Without Google/OpenAI provider keys, diagnosis and recommendation use the
deterministic rule-based fallback. Email and voice providers also have
simulated paths when credentials are absent. Policy and financial guards still
apply. Do not remove the LangGraph workflow or trained ML artifacts as part
of dependency cleanup without proving an alternative is active and tested.
