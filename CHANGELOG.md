# Changelog

## 1.0.0
### Product
- Multi-tenant portal: sign-up, CSV import, invoice list, scoring, review decisions, dashboard with six KPIs.
- Policy-gated recovery workflow (LangGraph) with a human review gate and a deterministic policy engine.
- Late-payment risk model (LightGBM) with per-invoice explanations; survival models for time-to-clear.
- AI assistant grounded in workspace facts, with voice input and read-aloud.

### Platform
- Transactional outbox, Kafka delivery, idempotent consumer, retries, dead letters, outbox reconciler and replay command.
- Pipeline health metrics exposed for Prometheus.
- Per-account sign-in lockout with exponential backoff; throttled account creation.
- Browser security headers; HTTP-only session cookie; allow-listed server-side proxy.
- CI (lint, tests with coverage gate, migrations, images, secret and dependency scans) and a gated deploy with smoke test.
