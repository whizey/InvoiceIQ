# InvoiceIQ operations

## Runtime and deployment boundary

The current release path builds a FastAPI image from `backend/Dockerfile` and a
Next.js image from `frontend/Dockerfile`. Both run as non-root users. The backend
build context admits runtime code, migrations, and the versioned model-risk
report; raw datasets and private environment files are excluded. The Docker
build verifies all 12 model binary/hash pairs before producing an image, so
missing Git LFS objects fail the build. CPU-only XGBoost 3.4.1 reduced the
backend image from 2.26 GB to 1.48 GB locally without changing the serving
model contract. With the same non-sample startup settings, local container
memory fell from 419.4 MiB to 192.9 MiB in the final build. Readiness was
3.34 s for the old image and 2.38–3.46 s across two optimized-image samples;
these samples do not establish a startup speedup. The optimized image used
315.2 MiB in synthetic-sample mode. The
frontend image built and its container served the sample readiness and finance
routes. These measurements are local, not hosted capacity guarantees.

`infra/compose.deploy.yml` is a loopback-only staging rehearsal, not a public
deployment. PostgreSQL persists in its own volume; Redis is optional for a
single-process sample but required for distributed locks across API replicas.
Kafka, provider LLMs, speech, and email remain optional. Disabling them uses
the code's documented deterministic or simulated paths; it does not prove
provider integration.

## Secrets, access, and migrations

Store database URLs, provider keys, analytics scopes, reviewer credentials,
operator tokens, and metrics tokens in a private secret store. Never expose
them through Next.js public variables. The API retains scoped analytics and
reviewer keys plus a hosted operator token for other mutations. The web login
exchanges a valid workspace key for an eight-hour, hashed, revocable viewer
session; logout revokes it server-side. This is a workspace credential model,
not per-person accounts. Rate limiting for the exchange is process-local; a
multi-replica authenticated product needs a shared limiter and user identity.

The portfolio Blueprint in `render.yaml` serves only six verified synthetic
companies from a dedicated `invoiceiq_sample` database. The initializer refuses
any other database name or existing unmarked schema. The public API runs in a
private service with `PUBLIC_READ_ONLY=true`, no scheduler, outbox dispatcher,
provider keys, or Redis. All external actions are blocked. Scripted agent
decisions are labeled `synthetic_fixture`; the web frame visibly identifies
simulated actions and deterministic AI fallback. This sample does not exercise
provider LLM calls, real communications, or human approvals of live actions.

The backend image does **not** migrate by default. `RUN_MIGRATIONS=true` enables
Alembic in the entrypoint; the existing local and loopback staging Compose
files set it explicitly. The sample Blueprint instead uses a pre-deploy command
that seeds only a fresh empty sample database and verifies the marker before
later migrations. For production data, review the Alembic revision and a
backup, test the revision on an isolated copy, and run migration as a separate
release operation before starting the API. Never point disposable tests at a
development or production database. `backend/scripts/run_full_tests.sh`
loads the private test environment and verifies each disposable target and
Alembic head before pytest starts.

The CI coverage gate remains 80% for deployable runtime modules. Offline
training, benchmark, audit, and report generators are excluded explicitly in
`backend/pyproject.toml`; runtime-imported survival helpers and serving code
remain included. Before those new offline modules were classified, the broad
package measurement was 61.49% with 616 passing tests. That number is not a
runtime coverage claim. Offline evidence is checked separately through
versioned evaluations, artifact hashes, and model contracts; it is not a
substitute for full test coverage of every experiment script.

## Health, metrics, and recovery

`/health` is liveness. `/health/ready` requires PostgreSQL and the serving
champion. `/internal/metrics` is token-protected in hosted environments when
metrics are enabled. It counts actual provider LLM attempts, validated
outcomes, latency, and deterministic fallbacks with bounded labels. Provider
token usage and cost are unavailable when structured responses omit usage
metadata. Request logs carry IDs and timings; model evidence in the
UI is recorded development evidence, not a fresh holdout. When
`INVOICEIQ_AGENT_TRACES=true`, successful graph nodes emit metadata-only
log events that correlate request and investigation IDs with node timing and
policy outcome; the tenant-scoped span API remains separate. No raw prompts or
invoice contents are added to these events. Review pending
approvals, outbox failures, database errors, and model load errors during an
incident. Disable schedulers and outbox dispatch before investigating repeated
side effects, then reconcile the audit and outbox state before re-enabling.

For a persistent hosted database, enable provider-managed backups and test a
restore into an **isolated** database. Keep the prior image and migration
revision for rollback. Code rollback can redeploy the prior image; a schema
rollback requires a reviewed migration or restoration plan and must not be
attempted automatically against live data.

## Hosting estimate and release gate

As checked on 2026-10-14, the prepared Render Blueprint uses a 2 GB private
API service (about $25/month), a 512 MB Next.js web service (about $7/month),
and a 256 MB paid Postgres database (about $6/month): roughly **$38/month**
before bandwidth, extra storage, taxes, and optional provider usage. The 2 GB
API tier leaves room beyond the measured 315 MiB idle container footprint;
peak traffic memory remains unmeasured. No Redis, Kafka, LLM, email, or speech
provider is required for this read-only sample. Render's free database expires
after 30 days and has no backups, so the Blueprint uses the smallest paid
database. Check current terms before approval:

- https://render.com/pricing
- https://render.com/docs/free
- https://render.com/docs/compute-plans

No public deployment or remote GitHub Actions run has been verified. Before
Blueprint sync, obtain owner approval for resource creation and cost, confirm
that the connected Git provider supplies the LFS model binaries, and review
database backup and retention settings. The image build fails if LFS binaries
are missing. After approval, verify the Render Blueprint remotely, migrate and
seed only its new sample database, check `/api/sample/ready`, run browser journeys
against the hosted URL, and confirm HTTPS. Roll back code by redeploying the
previous image; restore the sample database only from a verified isolated backup
if a schema or seed change requires it. Do not run database downgrades blindly.

### Hosted acceptance after approval

Set `INVOICEIQ_PUBLIC_SAMPLE_URL` to the approved `https://...onrender.com`
origin, then run these from the repository root. The scripts refuse HTTP and
never accept credentials or send mutations with valid identities:

```sh
python3 scripts/verification/hosted_sample_acceptance.py --url "$INVOICEIQ_PUBLIC_SAMPLE_URL"
cd frontend && node scripts/check-hosted-sample-browser.mjs
```

The HTTP script checks public TLS access, synthetic provenance, real finance
and case proxy responses, all invoice pages, billed/invoiced/open KPI
reconciliation, invoice filtering, and denied public write requests. Chromium
checks the rendered dashboard and invoice page without intercepting API
responses. Both must exit zero; neither has been run against a hosted service.

The private API has no public URL. From an authorized Render service shell,
verify `/health/ready`, migration revision, six synthetic companies, six
stored predictions with matching champion version, ML inference and SHAP,
historical coverage, request IDs, and read-only rejection at the API boundary.
Confirm that the frontend reaches the private service through `API_BASE_URL`
and that no backend address or secret appears in browser assets. Review Render
logs for database and model errors and verify the paid database's backup and
restore settings. Run normal workspace-key login, invalid-login, expiration,
logout, and cross-company tests in a separate guarded non-public test
environment; public sample mode intentionally has no login or live approval.
Remote CI, provider LLMs, and hosted telemetry are separate verification gates.
