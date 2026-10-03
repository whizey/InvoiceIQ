# AI Revenue Recovery Agent


An AI-assisted system that detects at-risk B2B revenue (overdue invoices), decides how to recover it, and executes that decision inside deterministic financial guardrails — instead of relying on fixed retry rules or manual finance follow-ups.

> **Status: under active development, built phase-by-phase.** This README tracks what's actually implemented (see [Build status](#build-status)), not the end-state vision. It is a **portfolio project on seeded, synthetic data** — no real payments, customers, or financial infrastructure are involved.

## The problem

Businesses lose revenue when B2B invoices go overdue. Most companies handle this with either rigid rule-based reminders or manual finance-team chasing — neither of which scales well or reasons about context (this customer usually pays on time vs. this one has a pattern of late payment, this amount needs escalation vs. this one doesn't).

This project builds a controlled, auditable agentic workflow that:

1. Detects overdue invoices and quantifies revenue at risk.
2. Scores recovery probability with a trained ML model (XGBoost, synthetic data).
3. Uses an LLM to diagnose the likely cause and recommend a next action.
4. Runs that recommendation through a **deterministic policy engine** before anything happens.
5. Executes the approved action through a mock tool (email, payment link, escalation).
6. Tracks the outcome — payment, promise-to-pay, or escalation — and loops or stops.
7. Records every decision in an audit trail and surfaces recovered revenue on a dashboard.

## Design principle

The core engineering decision in this project is keeping these four responsibilities strictly separate:

| Layer | Responsibility | Can it touch money or send anything? |
|---|---|---|
| **LLM** | Diagnose the situation, recommend one action from a fixed set | No |
| **ML (XGBoost)** | Score recovery probability / risk level | No |
| **Deterministic policy engine** | Approve, reject, or force-escalate the recommended action against hard business rules (reminder caps, cooldowns, value thresholds, recovery window) | Decides, doesn't act |
| **Tools** | Actually send the email / generate the payment link / escalate | Only after policy approval |

The LLM never calls a tool directly. Every recommendation is checked by code, not by prompting the model to "be careful."

```
Overdue Invoice
      │
      ▼
Revenue-at-Risk + Recovery Case
      │
      ▼
Load Customer Context ──▶ XGBoost Risk/Probability Score
      │
      ▼
LLM Diagnosis ──▶ LLM Intervention Recommendation
      │
      ▼
Deterministic Policy Engine  ◀── hard-coded rules, not prompted behavior
      │  (approved)
      ▼
Mock Tool Execution (email / payment link / escalate)
      │
      ▼
Outcome Recorded ──▶ Recovered? ──yes──▶ Close Case
      │no
      ▼
Recovery window still valid? ──no──▶ Escalate / Close
      │yes
      ▼
Next Allowed Action (loop back to Policy Engine)
      │
      ▼
Audit Trail + Dashboard Metrics  (every step above is logged here)
```

## Tech stack

| Area | Choice | Why |
|---|---|---|
| Backend | Python 3.12, FastAPI, Pydantic, SQLAlchemy (async) | typed, async, industry-standard for AI services |
| Database | PostgreSQL | relational integrity for financial/audit data; `NUMERIC`, never float, for money |
| Agent orchestration | LangGraph | explicit typed state machine — not an open-ended agent loop |
| LLM | OpenAI-compatible, structured output via Pydantic | swappable provider, no unstructured free-text decisions |
| ML | XGBoost | recovery probability / risk scoring, trained on synthetic data |
| Events | Kafka (local, Docker) | domain events (`invoice.overdue`, `payment.received`, ...); wired in once the core workflow is proven |
| Caching / state | Redis | idempotency, locks, scheduling metadata |
| Frontend | Next.js, TypeScript, Tailwind | dashboard + case detail views |
| Testing | Pytest | unit + integration coverage on the deterministic layers |
| Infra | Docker, Docker Compose | one-command local environment |

Full architecture writeup: [docs/architecture.md](docs/architecture.md).

## The ML model — and its honesty boundary

`recovery_score` / `recovery_probability` come from an XGBoost classifier ([backend/app/ml](backend/app/ml)), **trained entirely on synthetic data** ([synthetic_data.py](backend/app/ml/synthetic_data.py) documents the generative process — a hand-specified, directionally-sensible logistic function with noise, not real outcomes). It scores AUC 0.78 on a held-out synthetic test split — real discriminative power, but only over a synthetic relationship it was itself trained to reproduce. **This is not a production financial risk model** and isn't presented as one; it demonstrates that the pipeline (feature extraction from real Postgres data → trained model → score persisted to the case → audited) actually works end-to-end. Every case the detection engine creates gets scored automatically; nothing here is hand-authored the way the Phase 3 demo scenarios are.

## The recovery workflow — live, not just designed

`POST /recovery-cases/{id}/run` ([backend/app/agents/graph.py](backend/app/agents/graph.py)) advances a case through one full cycle of the diagram above: load context → re-score risk → LLM diagnosis → LLM intervention recommendation → **deterministic policy check** → execute the policy-approved action → record the outcome. It's genuinely wired, not mocked out — the policy engine really does override the agent's recommendation when a rule fires (e.g. a reminder proposed inside the cooldown window gets rejected and replaced with `WAIT`, logged in `policy_decisions` either way).

No `GOOGLE_API_KEY` (or `OPENAI_API_KEY`)? Diagnosis/intervention fall back to a deterministic rule-based agent automatically ([app/agents/llm_client.py](backend/app/agents/llm_client.py)) — the graph, policy engine, and audit trail are fully exercised either way. Add a Google AI Studio key to `backend/.env` to use Gemini (preferred), or an OpenAI key as an alternate; `agent_decisions.model_name` always records which path actually ran (`"rule-based-fallback"` or the real model name).

`POST /invoices/{id}/simulate-payment` stands in for a real payment webhook, so "customer pays → case closes" is actually demonstrable end-to-end.

A broken promise-to-pay that goes unfulfilled is treated as a hard fact, not left for the LLM to notice: `POST /recovery-cases/detect-overdue` (and the scheduler tick) also resolves any pending promise whose date has passed — marking it `FULFILLED` if the invoice got paid, `BROKEN` if not — and a broken promise **forces escalation** on the case's next cycle, overriding whatever the agent recommends, the same way the high-value/overdue rule does.

## Events (Kafka)

Every important fact the app produces — an invoice going overdue, a case opening, an action completing, a promise breaking, a case closing — is also broadcast as a domain event ([backend/app/events](backend/app/events)), Postgres-first: the DB write commits, *then* the event publishes, so a Kafka hiccup can never lose or block the underlying business fact. No `KAFKA_BOOTSTRAP_SERVERS` configured? Events are logged instead of published — same zero-setup fallback pattern as the LLM. `docker compose up` includes a real single-node Kafka broker (KRaft mode, no Zookeeper); `python -m app.events.consumer` is a standalone script that proves messages actually flow, since nothing in the app itself consumes them — this is a broadcast for future systems to plug into, not something the app depends on internally.

## Idempotency (Redis)

`POST /recovery-cases/{id}/run` and `POST /recovery-cases/detect-overdue` are locked ([backend/app/core/locks.py](backend/app/core/locks.py)) so two overlapping triggers of the same operation — a retried request, a double-click, the in-process scheduler — can't race and double-execute actions. The second caller gets `409 Conflict`, not a silent duplicate. Verified with genuinely concurrent requests: firing two `POST .../run` calls at the same case in parallel produced exactly one new action, not two, with one request returning `200` and the other `409`. No `REDIS_URL` configured? Falls back to an in-process lock — same pattern as the LLM and Kafka — safe for a single instance, though (honestly) it can't coordinate across multiple app instances the way real Redis does.

With `SCHEDULER_ENABLED=true` the API runs those same two operations on a timer (`app/services/scheduler.py`): detect overdue, then one recovery cycle per `OPEN`/`MONITORING` case. The dashboard polls every 5s (`LiveRefresh`) so you do not have to click Run. Manual buttons still work as overrides. CI leaves the scheduler off.

## The dashboard

Next.js (App Router, TypeScript, Tailwind) — the visual layer over everything above, at `http://localhost:3000`. The main view shows the KPIs from the brief (revenue at risk, recovered, recovery rate, active/escalated cases, average days overdue) plus a risk-level and current-action breakdown, and the full recovery-case table. Clicking a case opens its complete story: invoice and company detail, risk score, the AI's actual diagnosis and recommendation (with the model that produced it), the full action history with the policy decision behind each one, promise-to-pay status, communications, and the audit timeline — everything the brief's case-detail spec asked for, reading real data, not mocked screens.

Data fetching happens entirely server-side (Server Components) — the browser never calls the backend directly, so no CORS setup was needed. "Run recovery cycle," "Simulate payment," and "Run detection sweep" are Server Actions that call the same backend endpoints exercised throughout Phases 5-14, then revalidate the page.

## Observability

Every backend log line is one JSON object, and every HTTP request gets a request ID (generated, or reused from an incoming `X-Request-ID` header) that's bound to every log line emitted anywhere in that request's call stack — the risk engine, a LangGraph node, a Kafka publish failure — and echoed back in the response header, so one request's full story across every module is a single grep away. LangSmith tracing of the LangGraph workflow is a config-only opt-in (`LANGCHAIN_TRACING_V2=true` — LangChain already reports traces natively, no code change needed) — off by default, no account required to run the app. Deliberately **not** included: a full OpenTelemetry/collector setup — this is one service, not a distributed system needing cross-service trace correlation, and LangSmith already covers the observability that actually matters here.

## Why these boundaries (V1 scope)

This is a portfolio project, not a startup MVP or a production system, so scope is deliberately narrow and deep rather than broad and shallow:

- **One complete workflow** (B2B overdue invoice recovery), fully wired end-to-end, rather than partial coverage of many workflows.
- **Seeded, synthetic data** — realistic companies/invoices/payment histories covering low/medium/high risk and escalation scenarios, no real integrations.
- **Mock tools** for email, payment links, and escalation, built behind interfaces so Stripe/Resend/Twilio can be substituted later without touching the agent logic.
- Explicitly **not building**: real payment processing, real collections, voice/Hinglish agents, multi-agent swarms, or production-scale infra. See [docs/architecture.md](docs/architecture.md#v1-boundaries) for the full list and the planned V2–V4 direction.

## Project structure

```
backend/
  app/
    core/       # config, db session, logging
    models/     # SQLAlchemy models
    schemas/    # Pydantic schemas
    api/        # FastAPI routers
    services/   # risk engine, policy engine, case orchestration
    ml/         # XGBoost training + inference
    agents/     # LangGraph workflow, diagnosis/intervention agents, LLM client
    tools/      # mock email / payment-link / escalation tools
    events/     # event publisher abstraction (in-process now, Kafka later)
    seed/       # synthetic data + scenario generators
  tests/
infra/
  docker-compose.yml
docs/
  architecture.md   # full design: schema, event model, LangGraph state, API surface
frontend/
  app/              # Next.js App Router — dashboard + case detail pages, Server Actions
  components/       # Badge, MetricCard, CasesTable, AuditTimeline, ActionButton, ...
  lib/              # typed API client, formatting, badge-color mapping
```

## Getting started

Requires Docker (for Postgres) and Python 3.12+ and Node 20+ (for the dashboard).

```bash
./run.sh
```

Starts Postgres in Docker, sets up the backend virtualenv, applies migrations, and runs the API with auto-reload at `http://localhost:8000` **and** the dashboard with hot reload at `http://localhost:3000` — edit anything under `backend/app/` or `frontend/` and it updates live. Ctrl+C stops both; Postgres keeps running (`cd infra && docker compose down` to stop it too).

Once it's up, load some demo data and open the dashboard:

```bash
python -m app.seed.run   # from backend/, with the venv active — loads demo data
open http://localhost:3000
```

Prefer everything containerized instead (no local Python/Node needed, no hot reload)? `cd infra && docker compose up --build` — this now brings up Postgres, Kafka, Redis, the API, **and** the dashboard together. Full details for both paths are in [backend/README.md](backend/README.md) and [frontend/README.md](frontend/README.md).

Want a guided tour instead of exploring cold? [docs/demo.md](docs/demo.md) walks through every item on the project brief's own "definition of done" checklist — overdue detection, risk scoring, AI diagnosis/recommendation, the policy engine overriding the agent, promise-to-pay, escalation, and the audit trail — with the exact commands to reproduce each step.

## Build status

Built and verified incrementally, phase by phase — each phase has explicit acceptance criteria before the next one starts.

- [x] **Phase 1** — Project scaffold, Docker Compose, PostgreSQL, FastAPI health checks
- [x] **Phase 2** — Database models (all 12 tables) + Alembic migrations
- [x] **Phase 3** — Seed mock enterprise data (5 risk scenarios + 1 healthy account)
- [x] **Phase 4** — FastAPI read APIs (companies, invoices, recovery cases + detail/audit trail)
- [x] **Phase 5** — Revenue-at-risk & recovery-case engine (deterministic overdue detection + case creation) + dashboard metrics
- [x] **Phase 6** — XGBoost recovery-risk model (synthetic training data) wired into the detection engine
- [x] **Phase 7** — LangGraph recovery workflow, including the diagnosis agent, intervention agent, deterministic policy engine, and mock action tools (originally separate Phases 8-11 — built together since a graph with stub nodes isn't runnable; see [docs/architecture.md](docs/architecture.md))
- [ ] Phase 10 — Deterministic policy engine *(deepen: configurable thresholds)*
- [ ] Phase 11 — Mock recovery/action tools *(deepen: cleaner provider-swap abstraction)*
- [x] **Phase 12** — Outcome tracking + promise-to-pay: broken/fulfilled promise detection, forced escalation on a broken promise
- [x] **Phase 13** — Kafka event integration (7 domain events, log-fallback when unconfigured, standalone demo consumer)
- [x] **Phase 14** — Redis idempotency locks on both engine-trigger endpoints (409 on contention, in-process fallback when unconfigured)
- [x] **Phase 15** — Next.js dashboard (metrics, case table, full case detail, live actions)
- [x] **Phase 16** — Structured (JSON) logging with request-ID correlation + opt-in LangSmith tracing
- [x] **Phase 17** — Testing: 80 tests, ~94% coverage, GitHub Actions CI on every push
- [x] **Phase 18** — End-to-end demo: full checklist walked and verified against a fresh Docker stack ([docs/demo.md](docs/demo.md))

## Explicit non-goals for V1

No real financial transactions, real customer collections, production payment processing, legal collections automation, autonomous negotiation, voice/Hinglish agents, or production-scale infrastructure. This is a controlled simulation intended to demonstrate agentic-system design with proper guardrails, not a product.

## License

MIT
