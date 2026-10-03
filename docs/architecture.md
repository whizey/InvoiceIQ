# Architecture

This document is the design reference for the AI Revenue Recovery Agent (V1). It reflects decisions made before implementation started and is updated if a phase changes them.

## 1. Scope

V1 implements **one** end-to-end workflow — B2B overdue invoice recovery — on seeded/synthetic data with mock external tools. Failed payments/subscriptions, voice/Hinglish agents, and multi-agent swarms are explicitly out of scope (see [V1 boundaries](#v1-boundaries)).

## 2. Architectural principle

Four layers, strictly separated:

- **LLM** — reasoning only: diagnoses the case, recommends one action from a fixed enum. Never calls a tool directly.
- **ML (XGBoost)** — recovery probability / risk scoring. Trained on synthetic data; explicitly not a production financial model.
- **Deterministic policy engine** — plain Python/config rules (reminder caps, cooldowns, value thresholds, recovery window). Approves, rejects, or forces escalation on every proposed action. This is the only layer allowed to gate execution.
- **Tools** — mock implementations of email, payment link generation, escalation. Built behind interfaces so real providers (Stripe/Resend/Twilio) can replace them later without touching agent code.

```
Overdue Invoice → Revenue-at-Risk → Recovery Case
  → Load Customer Context → XGBoost Risk/Probability
  → LLM Diagnosis → LLM Intervention Recommendation
  → Policy Engine (deterministic gate)
  → Mock Tool Execution
  → Outcome Recorded → Recovered? → Close
                     → Recovery window valid? → No → Escalate/Close
                                               → Yes → Next Allowed Action (loop to Policy Engine)
  → Audit Trail + Dashboard Metrics (fed by every step above)
```

## 3. Domain model

```
companies 1──N contacts
companies 1──N invoices
invoices  1──N payments
invoices  1──N payment_events
invoices  1──1 recovery_cases        (one case per invoice in V1 — no reopening)
recovery_cases 1──N recovery_actions
recovery_cases 1──N agent_decisions
recovery_cases 1──N promise_to_pay
recovery_cases 1──N communication_logs
recovery_actions 1──N policy_decisions
recovery_cases 1──N audit_logs
```

## 4. Database schema

PostgreSQL. All monetary columns are `NUMERIC(14,2)` — never floating point.

| Table | Purpose | Key columns |
|---|---|---|
| `companies` | B2B customer accounts | id, name, industry, segment (SMB/MID_MARKET/ENTERPRISE) |
| `contacts` | People at a company | id, company_id fk, name, email, role, is_primary |
| `invoices` | Billable invoices | id, company_id fk, invoice_number (unique), amount_total, currency, amount_paid, due_date, status |
| `payments` | Payment records against invoices | id, invoice_id fk, amount, payment_date, method, status |
| `payment_events` | Append-only domain event log; doubles as the outbox for Kafka | id, invoice_id fk, event_type, payload jsonb, occurred_at |
| `recovery_cases` | The unit of work for recovery | id, invoice_id fk (unique), status, revenue_at_risk, recovered_amount, risk_score, risk_level, recovery_probability, recovery_window_deadline |
| `recovery_actions` | Actions proposed/executed per case | id, recovery_case_id fk, action_type, status, proposed_by, sequence_number, result jsonb |
| `agent_decisions` | Structured LLM outputs (diagnosis, recommendation) | id, recovery_case_id fk, stage, model_name, input_context jsonb, output jsonb, rationale |
| `promise_to_pay` | Tracked customer commitments | id, recovery_case_id fk, promised_amount, promised_date, status |
| `communication_logs` | Simulated outbound/inbound comms | id, recovery_case_id fk, contact_id fk, channel, direction, status, sent_at |
| `policy_decisions` | Policy engine verdict per action | id, recovery_action_id fk, policy_name, decision, reason |
| `audit_logs` | Canonical append-only timeline for a case | id, recovery_case_id fk, entity_type, entity_id, event_type, actor, description, metadata jsonb, occurred_at |

`agent_decisions.rationale` stores a concise explanation, never raw chain-of-thought.

Indexes: `invoices(due_date, status)`, `invoices(company_id)`, `recovery_cases(status)`, `recovery_actions(recovery_case_id)`, `promise_to_pay(status, promised_date)`, `audit_logs(recovery_case_id, occurred_at)`.

## 5. Event model — implemented (Phase 13)

`app/events/` defines `EventPublisher` (`app/events/publisher.py`) with two implementations, chosen automatically by whether `KAFKA_BOOTSTRAP_SERVERS` is configured — the same auto-fallback pattern as the LLM client (Phase 7):

- `LogEventPublisher` (default) — logs the event instead of publishing. No broker required to run the app.
- `KafkaEventPublisher` — publishes to a real Kafka broker (`apache/kafka:3.9.0`, KRaft mode, single node, in `infra/docker-compose.yml`), with a bounded 5s timeout so an unreachable broker can never hang the request that triggered the publish. Failures are logged, never raised — Postgres is always the source of truth, Kafka is a best-effort broadcast on top of it, published only after the triggering DB commit succeeds.

Topics actually published to (`app/events/topics.py`): `invoice.overdue`, `payment.received`, `recovery.case_created`, `recovery.action_completed`, `promise_to_pay.created`, `promise_to_pay.broken`, `recovery.case_closed`.

`app/events/consumer.py` is a standalone demo consumer (`python -m app.events.consumer`) proving messages actually flow — it is not part of the FastAPI app's request path, since V1 has no long-running consumer (decision #2 above still holds: nothing in the app waits on a Kafka message).

## 6. LangGraph workflow state

```python
class RecoveryState(TypedDict):
    case_id: UUID
    invoice_id: UUID
    company_id: UUID
    invoice_context: InvoiceContext
    customer_context: CustomerContext
    risk_score: float | None
    recovery_probability: float | None
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] | None
    diagnosis: DiagnosisResult | None
    recommended_action: InterventionAction | None
    policy_result: PolicyResult | None
    last_action: RecoveryActionType | None
    action_result: dict | None
    reminder_count: int
    days_since_opened: int
    promise_to_pay: PromiseToPayInfo | None
    case_status: RecoveryCaseStatus
```

Nodes: `initialize_recovery_case → load_customer_context → calculate_recovery_risk → diagnose_case → recommend_intervention → policy_check → execute_action → record_outcome → (payment_recovered? / recovery_window_valid?) → next_allowed_action` (loops back to `policy_check`).

One graph invocation advances a case by exactly one recovery cycle. Cycles are triggered by the in-process scheduler (`SCHEDULER_ENABLED`, `app/services/scheduler.py`) or still by `POST /recovery-cases/{id}/run`. There is no long-running Kafka consumer.

## 7. API surface

- `GET /companies`, `/companies/{id}`
- `GET /invoices`, `/invoices/{id}`, `/invoices/overdue`
- `GET /recovery-cases`, `/recovery-cases/{id}`
- `POST /recovery-cases/{id}/run` — advances the LangGraph workflow one cycle
- `GET /recovery-cases/{id}/audit-trail`
- `GET /dashboard/metrics`
- `GET /policies`

Seeding is a CLI script (`backend/app/seed`), not an API endpoint.

## 8. Key implementation decisions

1. **One recovery case per invoice, no reopening in V1.** Simplifies the state machine; reopening is a natural V2 extension.
2. **In-process scheduler for workflow cycles**, not a long-running Kafka consumer. Detection and `/run` stay lock-guarded HTTP entry points; the scheduler calls the same services. Off by default in CI.
3. **Structured LLM output via LangChain's `with_structured_output` + Pydantic** — no extra structured-output library.
4. **`payment_events` is the outbox table** for the eventual Kafka producer, avoiding a schema change in Phase 13.
5. **Default currency INR**, stored per invoice/transaction, no FX conversion logic in V1.

## V1 boundaries

Not built in V1: real financial transactions, real customer collections, production payment processing, legal collections automation, autonomous negotiation of payment terms, voice calling, Hinglish voice agent, multi-agent swarms, recommendation engines, production-scale infrastructure.

Planned direction (not built now):

- **V2** — Stripe integration, real email provider, failed-payment/subscription recovery, real-time webhooks.
- **V3** — advanced ML, experimentation/control groups, incremental revenue measurement, adaptive strategies, ERP integrations.
- **V4** — voice recovery, multilingual/Hinglish communication, more sophisticated enterprise collections workflows.
