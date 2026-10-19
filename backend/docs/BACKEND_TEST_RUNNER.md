# Disposable PostgreSQL backend verification

## Host Terminal setup

Run these commands on the Mac host from the repository. The setup script creates
only `invoiceiq-ci-disposable-db` on `127.0.0.1:55436`. It refuses to reuse or
replace a container with that name. It never acts on
`invoiceiq-phase1-test-db` or its persistent volume.

```sh
cd /Users/riya/Desktop/InvoiceIQ/backend
bash scripts/provision_ci_disposable_db.sh
```

The script writes `backend/.env.test` with mode 0600. The runner loads it
automatically, so no environment variables need to be exported by hand:

```sh
./scripts/run_full_tests.sh
```

Keep `.env.test` outside Git. Use `--coverage` for the coverage gate.

The setup script checks host total and currently reclaimable memory, Docker
Desktop's VM memory, and the 45,839 row CSV size before starting. It requires
at least 6 GiB host RAM, 3 GiB currently reclaimable, and 4 GiB Docker VM RAM.
The CSV measured 8.2 MiB in this workspace. PostgreSQL data is limited to a
2 GiB tmpfs mount, with no bind mount or persistent volume; `df -h` is printed
after migration for each database. The actual Docker VM memory could not be
read from the sandboxed environment, so **capacity remains unconfirmed until this script
runs on the host**. Stop if the memory check rejects the machine; do not raise
the tmpfs limit without checking available memory.

The script creates and migrates `invoiceiq_test`,
`invoiceiq_phase1_regression_test`, `invoiceiq_phase1_corpus_test`, and
`invoiceiq_phase1_replay_test`. It imports the exact public corpus into the
corpus database and prepares the three-case historical replay separately.
No development or production database is consulted. The current Alembic head
is `f7c0b57c9e11`.

The runner verifies the exact container name, PostgreSQL 16 image, disposable
label, loopback port binding, tmpfs mount and 2 GiB limit, and absence of
persistent or bind mounts. It compares database name, user, port, cluster
system identifier, database OID, and server start time over Docker `psql`
and the actual host TCP connection. A suffix or environment variable alone
cannot authorize writes. Each partition launches a separate pytest process;
Phase 5 uses the regression database and all remaining tests use
`invoiceiq_test` as their primary connection. SQLAlchemy secondary engines
are restricted to separately verified corpus and replay databases. The
runner checks migration heads and fixture counts before starting pytest.
GitHub Actions downloads the public CSV from a pinned upstream commit and
checks its SHA-256 before provisioning these same databases.

`DATABASE_URL` is the application's primary URL and must be set before
application imports. `TEST_DATABASE_URL` selects the primary test database.
`INVOICEIQ_PHASE7_DB_URL` selects the verified Phase 7 connection, currently
the primary disposable test database.
`PHASE1_TEST_DATABASE_URL` selects the corpus database, and
`PHASE1_REPLAY_DATABASE_URL` selects the separate historical replay database.
`REAL_DATASET_DATABASE_URL` is used during corpus import.
`PHASE1_SOURCE_DATABASE_URL` and `WORKER_DATABASE_URL` are used by standalone
replay and worker tools. The suite does not inherit them from a developer
shell; it supplies the verified corpus URL as its historical source.

Do not run other jobs against these databases while the suite is running.
Do not use the existing `invoiceiq-phase1-test-db` as a substitute. The setup
script does not remove its new container after use; cleanup, if desired, must
name only `invoiceiq-ci-disposable-db` and is outside the test command.

## Frontend checks on the host

From `/Users/riya/Desktop/InvoiceIQ/frontend`:

```sh
npm run lint
npm run typecheck
npm run build
npm test
npm audit --omit=dev --audit-level=high
npm run start -- -H 127.0.0.1 -p 3047
```

Leave that server running. In a second Terminal:

```sh
cd /Users/riya/Desktop/InvoiceIQ/frontend
CHROME_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test:loading
```

The browser suite uses five Playwright tests at `127.0.0.1:3047`. The Chrome
path avoids downloading a separate Playwright browser. The optional
`check-preserved-browser.mjs` script additionally needs a separately prepared
baseline and live fixture under `/private/tmp`; it was not part of this run.
Run the dependency audit with network access; an offline empty advisory cache
does not establish that the installed packages are free of known issues.

## Current verification (2026-10-14)

- Backend targeted readiness, authorization, AI/ML and safety tests:
  **45 passed, 1 deselected**, 1.53 s, with `UserWarning` treated as an error.
  The deselected prediction API test needs PostgreSQL telemetry.
- Backend Ruff: **passed with 0 findings** after syntax-preserving formatting,
  unused-import cleanup and annotations for intentionally late imports.
- Complete partitioned runner after the request-size guard and LLM telemetry:
  Phase 5 **2 passed** in 2.76 s; remaining **608 passed, 7 skipped, 2 warnings**
  in 246.96 s. Overall exit 0. The runner now supplies the verified corpus
  database as `PHASE1_SOURCE_DATABASE_URL`, so the historical source parity
  test runs automatically. Warnings are from Google GenAI's deprecated type
  alias and an intermittent asyncpg cancellation coroutine during public
  dataset parity; the latter remains under investigation.
- Frontend lint: exit 0, 0 errors, 1 existing Next.js navigation warning in
  `app/connect/page.tsx`. Type check: pass. Latest production build: pass;
  standalone server present, with no build warning in that run.
- Frontend unit tests: **5 passed, 0 failed, 0 skipped**, 81.65 ms on the latest rerun. The formatter
  test now asserts the actual preserved compact INR display (`₹1.25L` and
  `−₹1.25Cr`); formatter code and dashboard values are unchanged.
- Browser tests: **5 passed, 0 failed, 0 skipped** against a local production
  Next.js server using installed Chrome with elevated local browser access.
  CI now installs Playwright Chromium and runs the same journeys.

No Git push, deployment, dashboard layout change, model artifact rewrite,
financial calculation change, or existing-container modification occurred.
