# Testing

## Summary

| Layer | Tool | Scope |
|---|---|---|
| Backend | pytest, pytest-asyncio, coverage | 886 collected tests (about 120 files), run against a real PostgreSQL; 80% coverage gate in CI |
| Frontend unit and contract | Node test runner | Dashboard inventory and styles, portal contracts, CSV import, navigation, financial consistency, scenario data |
| Browser journeys | Playwright (Chromium) | Sign-up, import, decisions and the dashboard against a production build |
| Static checks | Ruff, ESLint, TypeScript | Style and types |
| Security | gitleaks, `pip-audit`, `npm audit`, privacy check | Secrets, dependencies, private files |
| AI safety | Offline agent-safety cases, guardrail tests | Prompt-injection screening, schema validation, policy behaviour |

## What the backend tests protect

- **Tenant isolation:** one company cannot read or mutate another's invoices, decisions, alerts or sessions; sample visitors cannot see each other's decisions.
- **Auth and sessions:** password hashing, revocation, expiry, role checks, that errors do not reveal whether an account exists, and sign-in lockout with backoff, per-account isolation and bounded memory.
- **Business rules:** the policy engine's ordering, reminder caps and cooldowns, forced escalations, and the review gate (nothing is sent until approved).
- **Data integrity:** CSV import validation, balances and the payment ledger, dashboard totals that reconcile, the balance bridge, and recovery summary semantics (latest decision wins, `RESET` clears).
- **Event pipeline:** replay produces no duplicate effects, late events do not overwrite newer ones, failures retry then dead-letter, poison messages skip retries, tenant scoping of alerts, pipeline metrics, and dead-letter replay.
- **ML integrity:** artifact verification, contract checks and feature-schema ordering.
- **Safety guards:** the sample-data tooling refuses to run on any database that is not explicitly approved, and test connections are checked against the disposable database's identity before any migration or fixture runs.
- **Observability and limits:** request-body limits, request ids in logs, public read-only mode.

## Running the backend tests

The suite uses a **disposable** PostgreSQL, never a developer database. Fixtures clear and reseed tables, so the connection guard (`scripts/test_database_safety.py`) refuses any destination that is not the approved test instance.

```sh
# see backend/docs/BACKEND_TEST_RUNNER.md for provisioning the disposable database
export TEST_DATABASE_URL="postgresql+asyncpg://<user>:<password>@127.0.0.1:<port>/<test-db>"
cd backend
alembic upgrade head            # against the test database
python -m pytest -q tests       # all tests
./scripts/run_full_tests.sh --coverage    # the CI path, with the 80% gate
```

A small group of tests needs an extra database URL (`INVOICEIQ_PHASE7_DB_URL`) and runs in CI.

## Running the frontend tests

```sh
cd frontend
npm ci
npm run lint && npm run typecheck && npm test
npm run build
npm run test:portal      # Playwright journey; needs a running production build
```

## CI migration test

CI applies migrations **up, down to base, then up again** to prove they are reversible and repeatable.

## Known gaps

- Hosted end-to-end browser journeys are not run against the live site; the post-deploy smoke test covers the site, a sample session and the main data endpoints.
- Voice features depend on browser support and a microphone, so they are verified manually.
