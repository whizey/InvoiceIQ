# API reference

Hosted at `https://riya1104-invoiceiq.hf.space`. FastAPI's `/docs` page is on when the environment allows it.

The browser doesn't call these directly; the Next.js app forwards an allow-listed set as `/api/portal/*`. Authenticated routes take `Authorization: Bearer iqp_...`.

## Health

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/health/db` | Database reachable |
| GET | `/health/ready` | Database and primary model available |

## Portal (`/portal`)

| Method | Path | Who | What it does |
|---|---|---|---|
| POST | `/portal/register` | anyone | Creates a user, a workspace and an owner membership, and returns a session |
| POST | `/portal/login` | anyone | Signs in. Doesn't say whether the email exists. Five failures lock the account for 30 s, doubling to 15 min (`429` with `Retry-After`) |
| POST | `/portal/showcase?ledger=kaveri\|public-2016` | anyone | A visitor session on one of the public workspaces |
| GET | `/portal/me` | session | Who you are, your workspace and role, and the workspace's `as_of` date if it has one |
| GET, PATCH | `/portal/settings` | session | Name, company, password |
| DELETE | `/portal/session` | session | Sign out |
| POST | `/portal/erase` | owner | Deletes all workspace data (`scope=data`) or the account too (`scope=account`, needs the password) |
| GET | `/portal/follow-ups` | session | Ranked chase list, due this week, disputed, cash forecast and the model comparison |
| GET | `/portal/invoices` | session | `q`, `page`, `page_size`, `sort`, and `status` (`all`, `overdue`, `awaiting`, `escalated`) |
| GET | `/portal/overview` | session | Totals, the first 100 invoices and recent decisions |
| POST | `/portal/import-preview` | session | What a Tally or Zoho export would import; writes nothing |
| POST | `/portal/import-file` | owner, admin | Imports the ready rows and scores the workspace |
| GET | `/portal/example-export?format=zoho\|tally` | session | An example export to try the importer with |
| POST | `/portal/import` | owner, admin | Older JSON import, 250 rows |
| POST | `/portal/decisions` | reviewer and up | `APPROVE`, `REJECT`, `ESCALATE` or `RESET` with a reason |
| GET, POST | `/portal/notes` | session (POST: reviewer and up) | Notes on an invoice |
| POST | `/portal/reminder-draft` | reviewer and up | Checks the reminder rules, then drafts a message |
| GET, POST | `/portal/reminders` | session (POST: reviewer and up) | Reminders sent, with the payment that followed; POST logs one |
| GET | `/portal/alerts` | session | Approval alerts |
| GET | `/portal/recovery-summary` | session | Review counts, amounts and decisions per week |
| GET | `/portal/dashboard`, `/portal/export` | session | Dashboard data for a period, or the same as CSV |
| GET | `/portal/intelligence`, `/portal/intelligence/{id}` | session | Scores, and one invoice's drivers |
| POST | `/portal/score` | owner, admin | Scores up to 2,000 invoices |
| POST | `/portal/assistant`, `/portal/assistant-stream` | session | An answer, or progress events then the answer; 8 a minute |
| GET | `/portal/assistant-reliability`, `/portal/assistant-traces` | session | What the reliability page shows |
| POST | `/portal/transcribe` | session | Audio to text for the assistant's microphone button |

Common errors: `401` no or expired session, `403` not allowed for your role or workspace, `404` not in your
workspace, `409` duplicate invoice or a reminder inside the three-day window, `422` bad input, `429` rate limited,
`503` model unavailable.

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
