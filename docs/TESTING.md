# Testing

## Summary

| Layer | Tool | Scope |
|---|---|---|
| Backend | pytest, pytest-asyncio, coverage | 937 collected tests (about 126 files), run against a real PostgreSQL; 80% coverage gate in CI |
| Frontend unit and contract | Node test runner | Dashboard inventory and styles, portal contracts, CSV import, navigation, financial consistency |
| Browser journeys | Playwright (Chromium) | Sign-up, import, decisions, follow-ups, the model page and the dashboard against a production build |
| Static checks | Ruff, ESLint, TypeScript | Style and types |
| Security | gitleaks, `pip-audit`, `npm audit`, privacy check | Secrets, dependencies, private files |
| AI safety | Offline agent-safety cases, guardrail tests | Prompt-injection screening, schema validation, policy behaviour |

## What the backend tests cover

- Tenant isolation: one company can't read or write another's invoices, decisions, alerts or sessions. Public-workspace visitors can't see each other's decisions, notes or reminders.
- Auth: password hashing, session expiry and revocation, role checks, and that a wrong password and an unknown email look identical. Sign-in lockout and its backoff.
- Follow-ups: the ranking, the baseline vs model comparison and calibration, the cash forecast's conditioning on invoice age and its track record.
- Import: Tally and Zoho column detection, Indian dates and Dr/Cr amounts, what gets skipped and why.
- Reminders: the policy rules, rejecting drafts with invented numbers or that were cut off, and the payment-outcome log.
- Business rules: the policy engine's ordering, reminder caps and cooldowns, forced escalations, the review gate.
- Data integrity: import validation, the payment ledger, dashboard totals, the balance bridge, recovery-summary status counts.
- The event pipeline: no duplicate effects on replay, late events don't overwrite newer state, failures retry then dead-letter, tenant scoping.
- The ML models: artifact verification, contract checks, feature-schema order.
- Safety guards: seeding tools refuse any database that isn't explicitly approved, and tests check the disposable database's identity before touching it.

## Running the backend tests

The suite runs against a disposable PostgreSQL, never a developer database. Fixtures clear and reseed tables, so `scripts/test_database_safety.py` refuses any target that isn't the approved test instance.

```sh
# see backend/docs/BACKEND_TEST_RUNNER.md for setting up the disposable database
export TEST_DATABASE_URL="postgresql+asyncpg://<user>:<password>@127.0.0.1:<port>/<test-db>"
cd backend
alembic upgrade head            # against the test database
python -m pytest -q tests       # everything
./scripts/run_full_tests.sh --coverage    # what CI runs, with the 80% gate
```

A small group of tests needs an extra database URL (`INVOICEIQ_PHASE7_DB_URL`) and only runs in CI.

## Running the frontend tests

```sh
cd frontend
npm ci
npm run lint && npm run typecheck && npm test
npm run build
npm run test:portal      # Playwright journey, needs a running production build
```

## CI migration test

CI applies migrations up, down to base, then up again, to catch anything that isn't reversible.

## Known gaps

- The post-deploy smoke test checks the live site, a session and the main data endpoints, but the full browser journey only runs against a local build.
- Nothing exercises a second API instance, so the shared-nothing assumptions in the rate limiter and read cache aren't tested under real concurrency.
