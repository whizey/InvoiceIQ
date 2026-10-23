# Changelog

## Unreleased
- Follow-ups: a ranked chase list (amount × chance of paying late × days overdue) with a one-line reason per invoice, now the landing page.
- A per-customer baseline score, used instead of the ML model unless the model beats it on that workspace's own history; both are shown with calibration on the Model page.
- A weekly cash forecast with a range, checked against its own track record.
- Importing Tally and Zoho Books exports (CSV or Excel), with a preview of what will be skipped and why.
- Reminder drafts (email or WhatsApp, English or Hinglish) that can only use facts from the invoice; a sent-reminder log that tracks whether a payment followed.
- A second public workspace built from real, anonymised invoices in a public dataset.
- Delete my data, a privacy page, optional Sentry error tracking, nightly encrypted backups.

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
