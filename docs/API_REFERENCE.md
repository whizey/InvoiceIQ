# API reference

Base URL (hosted): `https://riya1104-invoiceiq.hf.space`. Interactive OpenAPI documentation is served by FastAPI at `/docs` when enabled for the environment.

In the hosted product the browser never calls these routes directly. The Next.js app proxies the allow-listed `/api/portal/*` subset and forwards the session token. Authenticated routes expect `Authorization: Bearer iqp_...`.

## Health

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/health/db` | Database reachable |
| GET | `/health/ready` | Database and primary model available |

## Customer portal (`/portal`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/portal/register` | none | Create a user, a workspace and an owner membership; returns a session |
| POST | `/portal/login` | none | Sign in. Errors do not reveal whether an account exists. After 5 failed attempts the account is locked for 30 seconds, doubling up to 15 minutes (`429` with `Retry-After`) |
| POST | `/portal/showcase` | none | Start an isolated session on the sample workspace |
| GET | `/portal/me` | session | Current identity, workspace and role |
| GET / PATCH | `/portal/settings` | session | Read or update display name and company |
| DELETE | `/portal/session` | session | Revoke the current session |
| GET | `/portal/overview` | session | Headline totals, first 100 invoices, recent actions |
| GET | `/portal/invoices` | session | Search, sort and paginate invoices (`q`, `page`, `page_size`, `sort`) |
| POST | `/portal/import` | owner/admin | Import up to 250 invoices as JSON |
| POST | `/portal/decisions` | owner/admin/reviewer | Record `APPROVE`, `REJECT`, `ESCALATE` or `RESET` with a reason |
| GET | `/portal/alerts` | session | Approval alerts for the current scope |
| GET | `/portal/recovery-summary` | session | Recovery KPIs, status breakdown, weekly decisions |
| GET | `/portal/dashboard` | session | Dashboard data for the selected period and filters |
| GET | `/portal/export` | session | The same selection as CSV |
| GET | `/portal/intelligence` | session | Scored invoices and portfolio view |
| GET | `/portal/intelligence/{invoice_id}` | session | One invoice's risk and its drivers |
| POST | `/portal/score` | owner/admin | Score up to 2,000 invoices at posting time |
| POST | `/portal/assistant` | session | Grounded plain-language answer; 8 requests per minute |
| POST | `/portal/transcribe` | session | Audio to text |

Typical errors: `401` missing or expired session, `403` role or scope not allowed, `404` invoice not in this workspace, `409` duplicate invoice number on import, `422` validation, `429` sign-in lockout, or the assistant or transcription limit reached, `503` assistant model unavailable.

## Recovery workflow (legacy single-company surface)

| Method | Path | Purpose |
|---|---|---|
| POST | `/recovery-cases/detect-overdue` | Open cases for overdue invoices and update promises |
| GET | `/recovery-cases` | List cases |
| GET | `/recovery-cases/{case_id}` | Case detail |
| POST | `/recovery-cases/{case_id}/run` | Run the agent workflow once |
| POST | `/recovery-cases/{case_id}/send-reminder-email` | Send a reminder to the configured sample address |
| GET | `/recovery-cases/{case_id}/audit-trail` | Audit rows |
| GET | `/recovery-cases/{case_id}/agent-operations` | Agent node history |
| GET | `/recovery-cases/pending-reviews` | Actions waiting for a human |
| POST | `/recovery-cases/actions/{action_id}/approve` and `/reject` | Resolve a held action |
| POST | `/recovery-cases/{case_id}/voice/start` and `/voice/turn` | Voice-call sample |

## Invoices and companies (legacy)

| Method | Path |
|---|---|
| GET | `/companies`, `/companies/{company_id}` |
| POST / GET | `/invoices`, `/invoices/overdue`, `/invoices/{invoice_id}` |
| POST | `/invoices/{invoice_id}/simulate-payment` (a simulation only) |

## Dashboards (legacy and reviewer)

| Method | Path |
|---|---|
| GET | `/dashboard/metrics`, `/dashboard/policy-overrides`, `/dashboard/analytics`, `/dashboard/calendar` |
| GET | `/dashboard/preserved`, `/dashboard/preserved/export` |
| GET | `/dashboard/workspace/cases`, `/cases/{case_id}`, `/reviews`, `/review-history`, `/review-candidates` |
| POST | `/dashboard/workspace/reviews/{action_id}/{decision}`, `/dashboard/workspace/review-requests` |

## Machine-learning endpoints (`/ml`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/ml/models`, `/ml/model-risk`, `/ml/dashboard`, `/ml/offline-evaluations` | Model catalogue and evaluation status |
| GET | `/ml/payment-risk/features` | Ordered feature schema |
| POST | `/ml/payment-risk/predict` | Late-payment probability with TreeSHAP drivers |
| GET | `/ml/process-survival/features`, `/ml/process-survival/landmarks` | Survival model schema and landmarks |
| POST | `/ml/process-survival/predict` | Purchase-to-pay clearance probabilities |

## Other

| Method | Path | Purpose |
|---|---|---|
| POST / DELETE | `/auth/sessions`, `/auth/sessions/current` | Legacy workspace session exchange |
| GET | `/analytics/...` | Public-dataset analytics |
| GET | `/internal/metrics` | Prometheus metrics, token protected |
