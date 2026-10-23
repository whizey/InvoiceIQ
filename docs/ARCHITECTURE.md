# Architecture

## System context

```mermaid
flowchart TB
  subgraph Client
    B[Browser]
  end
  subgraph Vercel
    W[Next.js 16 app\nserver components + API proxy]
  end
  subgraph HF[Hugging Face Space]
    API[FastAPI]
    C[Event consumer]
  end
  subgraph Data
    PG[(PostgreSQL 16\nSupabase)]
  end
  subgraph AI
    GEM[Gemini, Groq]
    ML[Versioned ML artifacts\nLightGBM, survival models]
  end
  B -->|HTTPS| W
  W -->|allow-listed routes| API
  API --> PG
  API --> ML
  API --> GEM
  API -->|outbox rows| PG
  C --> PG
  KAFKA[(Kafka-compatible broker\noptional)] <--> API
  KAFKA <--> C
```

There are two deployable parts. The web app serves pages and forwards API calls; the API holds all the business rules. The browser can't reach the database or the API directly.

## The request path

1. The browser requests a page or calls `/api/portal/...` on the Next.js app.
2. The Next.js catch-all route checks the path against an allow-list, checks the request `Origin`, and reads the session token from an HTTP-only cookie.
3. It forwards the call to FastAPI with the token as a bearer credential.
4. FastAPI's `identity` dependency looks up the session, loads the workspace, verifies membership (for customer workspaces) and returns a scoped context. Every query that follows is filtered by that workspace.
5. Read endpoints use a short per-workspace cache. Any write bumps the workspace's version, so the next read is fresh.

## Layers inside the backend

| Layer | Folder | Responsibility |
|---|---|---|
| API | `backend/app/api/` | HTTP routes, request validation, response shapes |
| Core | `backend/app/core/` | Config, database session, auth and scoping helpers, request limits, observability, locks |
| Services | `backend/app/services/` | Follow-up ranking, late reasons, cash forecast, import, reminders, dashboards, policy engine, review gate |
| Agents | `backend/app/agents/` | LangGraph workflow, prompts, guardrails, LLM client |
| ML | `backend/app/ml/` | Training, evaluation, calibration, serving and drift monitoring for the models |
| Events | `backend/app/events/` | Outbox, publishers, consumer, handlers, in-process delivery, topics |
| Models | `backend/app/models/` | SQLAlchemy tables |
| Tools | `backend/app/tools/` | Email and read-only query helpers used by the older workflow |
| Seed | `backend/app/seed/` | Deterministic workspace seed and dataset loaders |

## Data model

33 tables in five groups, built by 25 Alembic migrations:

- Portal: `portal_workspaces` (with an optional `as_of` date), `portal_users`, `portal_memberships`, `portal_sessions`, `portal_invoices`, `portal_payments`, `portal_case_events`, `portal_notes`, `portal_reminders`, and `assistant_traces`
- Older recovery workflow: `companies`, `contacts`, `invoices`, `payments`, `payment_events`, `recovery_cases`, `recovery_actions`, `agent_decisions`, `policy_decisions`, `promise_to_pay`, `communication_logs`, `audit_logs`
- Analytics and model provenance: `analytics_history_state`, `invoice_state_snapshots`, `case_state_snapshots`, `inference_records`, `dataset_imports`, `offline_evaluations`
- Events: `outbox_events`, `processed_events`, `case_risk_latest`, `approval_alerts`, `dead_letter_events`

A few things worth knowing:
- Portal records are tenant-scoped by `workspace_id`, and foreign keys cascade on workspace deletion.
- Sessions store only a SHA-256 digest of the token, never the token itself.
- On the public workspaces, decisions, notes and reminders carry the visitor's session digest, so visitors only see their own. Rows tagged `showcase-baseline` are the shared history everyone sees.
- Payments are a dated ledger, so collections can be reported by day.

## Two deliberately separate API surfaces

The repository holds an older single-company recovery API (`/companies`, `/invoices`, `/recovery-cases`, `/dashboard`, `/ml`) and the multi-tenant portal API (`/portal`). A guard middleware denies the legacy routes on a public portal deployment, because portal identities must never grant access to the older single-workspace API.

## Design decisions and trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| Deterministic policy gate between the AI and any action | Language models can be wrong or manipulated; rules are testable and auditable | Policy thresholds are module constants today; making them configurable per segment is future work |
| Transactional outbox for events | A decision and its event commit together or not at all, so nothing is announced that did not happen | At-least-once delivery, so consumers must deduplicate (they do) |
| Same handlers for broker and in-process delivery | The product behaves identically with or without Kafka | Without a broker, effects are applied inline after commit rather than asynchronously |
| Short-lived read cache with write invalidation | Cuts repeated aggregate queries | Cache is per process; scaling to several API instances would need a shared cache |
| Static reference dashboard in an iframe, fed by a data adapter | Preserves a reviewed visual design while data stays live | The iframe auto-sizes and hides its own sidebar on small screens |
| Artifact fingerprinting for models | Prevents silently serving a changed model | Artifacts live in Git LFS and must be fetched in CI and on deploy |

## Observability

Logs are JSON with a request id. Prometheus metrics (token-protected), LangSmith tracing and Sentry error reports are all optional and off unless configured. The assistant writes its own traces to `assistant_traces`.

See also: [BACKEND.md](BACKEND.md), [EVENT_PIPELINE.md](EVENT_PIPELINE.md), [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md).
