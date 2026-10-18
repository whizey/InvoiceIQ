# InvoiceIQ

[![CI](https://github.com/whizey/InvoiceIQ/actions/workflows/ci.yml/badge.svg)](https://github.com/whizey/InvoiceIQ/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

InvoiceIQ is a multi-tenant SaaS for B2B invoice monitoring and recovery. It
combines invoice and payment records, a late-payment risk model with
per-invoice explanations, a policy-gated LangGraph recovery workflow with a
human review gate, an event pipeline on Apache Kafka, and a live financial
dashboard with a grounded AI assistant.

**Live:** https://invoiceiq-ai.vercel.app (open the sample workspace, no sign-up needed).

| | |
|---|---|
| Web | Next.js 16 on Vercel |
| API | FastAPI on a Hugging Face Space |
| Data | PostgreSQL 16 on Supabase |
| Events | Transactional outbox, Kafka, idempotent consumer, dead letters |
| Delivery | GitHub Actions CI, gated deploy with a smoke test |

Decisions are in [docs/adr](docs/adr), changes in [CHANGELOG.md](CHANGELOG.md), and
how to contribute in [CONTRIBUTING.md](CONTRIBUTING.md). InvoiceIQ does not
process bank payments, and model forecasts are predictions, not guarantees.

## Architecture

```mermaid
flowchart LR
  Browser[Browser] --> Web[Next.js web]
  Web -->|server-side API calls| API[FastAPI]
  API --> DB[(PostgreSQL)]
  API --> Redis[(Redis locks)]
  API --> Graph[LangGraph and policy]
  Graph --> DB
  API --> Models[Versioned ML artifacts]
  API --> Outbox[Transactional outbox]
  Outbox --> DB
```

The frontend serves four retained dashboard pages: overview, invoices, cases,
and approvals. The Next.js server proxies allowlisted backend endpoints and
keeps the workspace credential in an HTTP-only cookie. FastAPI enforces
company scopes on financial reads and recovery writes; reviewers have separate
credentials and company scopes. Hosted writes require an operator credential.
The browser does not need cross-origin API access. PostgreSQL stores invoices,
payments, cases, audits, inference records and outbox events. Redis coordinates
concurrent recovery actions. In the single-process fallback, locks are
in-process. The outbox can use its log publisher without Kafka.

The stack is Python 3.12+, FastAPI, SQLAlchemy/asyncpg, Alembic, PostgreSQL 16,
LangGraph, LightGBM and the evaluated model artifacts; Next.js 16, React 19,
TypeScript, and Playwright are used for the web application and browser checks.
The model artifacts and their calibration policies are versioned and verified
without retraining at startup.

The B2B champion predicts late payment from an ordered feature schema and
returns native TreeSHAP explanations. BPI survival estimates a separate
purchase-to-pay process-clearance target; its probabilities are not customer
payment or recovery estimates. Evaluation and calibration reports remain in
`backend/app/ml/artifacts`, with methodology and limitations in
`backend/EVALUATION_PROTOCOL.md` and [the architecture guide](docs/ARCHITECTURE.md).

## Local setup

Requirements: Docker Desktop, Python 3.12+, Node.js 22 and npm. For ordinary
local development, `./run.sh` starts the local Compose database, runs migrations,
and launches the API and web server. It uses local development data and is **not
an integration-test database**. See [backend details](backend/README.md) for
manual startup and endpoint documentation. Never run the seed or test fixtures
against a database containing valuable data.

For guarded integration tests, provision the separate disposable instance in
[the backend test guide](backend/docs/BACKEND_TEST_RUNNER.md). It uses tmpfs and
its own host port. Run `backend/scripts/run_backend_tests.py` only after its
Docker/PostgreSQL identity checks pass. The Phase 5 regression tests and the
remaining tests run in separate Python processes and databases.

Frontend checks from `frontend/`:

```sh
npm ci
npm run lint
npm run typecheck
npm run build
npm test
```

The Playwright browser suite needs a running production build at
`127.0.0.1:3047`; the exact host commands are in the backend test guide.

## Release configuration

`infra/compose.deploy.yml` is a **loopback-only staging rehearsal** with web,
FastAPI, PostgreSQL and Redis. It is not a public deployment. Start from
`infra/.env.deploy.example`, generate independent random credentials, and put
the real values only in Git-ignored `infra/.env.deploy`. The deployed database
must be migrated. The backend image defaults to no migration; the local and
staging Compose files explicitly set `RUN_MIGRATIONS=true` to preserve their
existing behavior. Run production migrations as a separate reviewed step.
Configure `ANALYTICS_API_KEYS`, `REVIEWER_API_KEYS`, and
`INVOICEIQ_REVIEWER_COMPANY_SCOPES` using actual company UUIDs. The example
UUID is a placeholder and grants access to no real company. Never expose an
operator or reviewer credential as a browser environment variable.

For a public deployment, supply an HTTPS hostname, a persistent PostgreSQL
backup plan, and an access-control boundary for the web application. Keep the
API bound privately or behind an authenticated proxy. Set explicit analytics
and reviewer scopes, confirm that the appropriate data has been reconciled,
and enable scheduled recovery only after workflow checks. Gemini/OpenAI, email,
and voice credentials are optional: without them, the deterministic reasoning
and simulated communication paths remain available. CORS is intentionally not
opened for arbitrary browser origins; the existing Next.js server proxies API
requests.

Release steps, current limits, recovery, and cost estimates are in
[the operations guide](docs/OPERATIONS.md). The hosted setup is described in
[the SaaS deployment guide](docs/DEPLOY_SAAS.md).

## Visual reference

The reference dashboard pages live in `frontend/public/reference/{overview,invoices,cases,approvals}`
and are fed live data by the portal. Automated tests hash their CSS and check
headings, KPI labels and table columns. A hosted smoke test runs after every
deploy.

## Safety and limitations

InvoiceIQ does not process bank payments. The payment simulation endpoint is a
sample operation. ML outputs are predictive associations and do not replace
financial reconciliation. The `PUBLIC_READ_ONLY` mode has separate data and
write restrictions. Existing repository history and the current working tree
must pass the privacy checks before publication. Current blockers and exact
results are recorded in [release status](docs/RELEASE_STATUS.md).
