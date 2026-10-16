# InvoiceIQ public SaaS — feature-branch implementation

## Scope

Additive customer-portal tables do **not** modify legacy invoices, payments, KPI cards,
charts, dashboard adapters or financial rules. The original dashboard remains at `/`.
New product routes: `/home`, `/signup`, `/login`, `/platform`.

The portal is a separate integration slice, not a declaration of launch readiness.
Account signup is server-validated and backed by an 8-hour revocable session.
Only the account's own workspace ID may be used by portal APIs. Showcase sessions
have per-visitor decision histories and no permission to reach customer or legacy
invoice tables.

## Deployment rules

- Deploy backend with `INVOICEIQ_PORTAL_ENABLED=true` **only after** the portal
  migration is applied on a **nonproduction** integration database.
- Production requires rate limits, email verification/password resets, invitations,
  payment integrations, security testing and validated tenant-scoped upload paths.
- The portal database schema is append-only relative to legacy financial tables.
- The public showcase must use its own PostgreSQL database named `invoiceiq_sample`.
  Run `alembic upgrade head` there, then **explicitly** run
  `INVOICEIQ_PORTAL_SHOWCASE=true python -m scripts.provision_portal_showcase`.
  Provisioning refuses database names other than `invoiceiq_sample`,
  `invoiceiq_sample_test`, or `invoiceiq_portal_test`.
- For a showcase backend, `PUBLIC_READ_ONLY` must remain **false** because the
  portal writes isolated audit decisions, while `INVOICEIQ_PORTAL_ENABLED=true`
  blocks all legacy financial API writes. This is **not** the old public financial
  dataset deployment mode.
- The frontend expects a server-only `INVOICEIQ_API_URL` or `API_BASE_URL`.
  Never expose bearer tokens or database credentials in `NEXT_PUBLIC_*`.
- Do not point the showcase backend at a customer or historic local database.
  Do not run migrations or seed scripts against live Neon databases in CI.

## Currently verified functionality / limitations

The workspace API computes invoiced/open/overdue/collected totals from isolated
invoice records; all amounts reconcile against each record's amount_paid.
The `/portal/decisions` endpoint persists auditable review decisions scoped to
a visitor or member, but intentionally does not send messages or mutate the
legacy recovery workflow.

The portal is **not production-ready** until account email verification, shared
auth rate limiting, password recovery, strong access auditing, customer invoice
import, isolation integration tests, UI/browser tests and deployment checks pass.
Model-risk inference is not fabricated: showcase invoices are unscored until a
verified model computes results.

Do not merge this branch to production solely because compilation succeeds.

Payment posting is recorded in portal_payments with explicit dated entries. Imports require payment_date when amount_paid > 0; no invented collection date is allowed.
