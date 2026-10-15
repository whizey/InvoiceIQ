# InvoiceIQ implementation status (staged-source compatible patch)

This implementation extends the **2026-10-09 staged Git archive** of InvoiceIQ.
No raw datasets, trained model bytes, Git history, runtime credentials, or personal assistant files are bundled.

## Implemented / provided

- Backend CI, frontend CI (already in source), image builds, read-only CI permissions, and selective ML artifact Git LFS downloads (no entire raw BPI dataset).
- Repository filename privacy checker, secret-history workflow, dependency audit workflow, focused AI-safety evaluation workflow, manual artifact SHA/manifest verification workflow.
- Guardrails at LLM input/structured output and action execution boundaries, approved/rejected and pending-human-approval handling, and hosted write-key middleware.
- Redis distributed lock fail-closed behavior on hosted environments when Redis is absent or unreachable.
- Structured request logs and request IDs already existed. Adds optional low-cardinality Prometheus `/internal/metrics` (off by default, authenticated in hosted deployments).
- Offline PSI/missingness drift checker; no automated live drift data ingestion.
- Localhost-bound Docker Compose **staging rehearsal** and read-only smoke check. Existing `infra/docker-compose.yml` for local development is untouched.

## Must finish before public production

1. **Real user auth, authorization, tenant isolation and CSRF protection.** Hosted mutation-key protection is a temporary server-to-server gate, **not login**. The frontend is NOT safe for public exposure. Staging binds only 127.0.0.1 and is intended for local testing/SSH tunnel only.
2. Add a real protected data-serving or ingestion path for scheduled drift monitoring; a PSI CLI is not a scheduled model-drift alert or performance assessment.
3. Add durable event-outbox, idempotency keys for external side effects, Kafka dead-letter handling, and a tested multi-instance worker topology. Kafka and Celery are not proven production-ready.
4. Add OpenTelemetry collector/exporter and production alerting, real model registry/promotion, complete document processing, automated safe deployment/rollback.
5. Inspect historical `frontend/AGENTS.md` and `frontend/CLAUDE.md` in existing Git commits; staged deletions do not remove historical content. Confirm raw dataset redistribution rights before publishing.
6. Run tests and builds with all local saved model binaries fetched from Git LFS. This ZIP contains only their LFS pointers and can't prove the complete production model path here.

## Apply

```sh
cd ~/Desktop/InvoiceIQ
# Review the patch; check first against your current staged archive:
git apply --check "$HOME/Desktop/InvoiceIQ-current-compatible.patch"
git apply "$HOME/Desktop/InvoiceIQ-current-compatible.patch"

# Test using your existing ISOLATED TEST DATABASE; never use development DB:
set -a; source "$HOME/.config/invoiceiq/test.env"; set +a
(cd backend && .venv/bin/python -m pytest -q)
(cd frontend && npm run lint && npx tsc --noEmit && npm run build)
python3 scripts/verification/check_repo_privacy.py
python3 scripts/verification/check_model_artifacts.py --verify-binaries
```

## Local-only staging rehearsal

Create `infra/.env.deploy` OUTSIDE Git from the provided template and replace placeholders using unique credentials. Never commit it. Confirm it is ignored.

```sh
cd ~/Desktop/InvoiceIQ
cp infra/.env.deploy.example infra/.env.deploy
# Open infra/.env.deploy in your editor and set unique values
# Verify ignore behavior:
git check-ignore infra/.env.deploy

docker compose --env-file infra/.env.deploy -f infra/compose.deploy.yml config --quiet
docker compose --env-file infra/.env.deploy -f infra/compose.deploy.yml up -d --build
python3 scripts/verification/smoke_http.py
```

`ENV=staging` intentionally rejects unauthenticated writes; the existing unauthenticated frontend does not become a public app merely because Compose starts.
Do not run `docker compose down -v` unless you explicitly want to destroy the staging database volume.

## Offline drift demo

```sh
cd backend
.venv/bin/python -m app.ml.drift_monitor \
  --reference ../data/processed/primary/splits/train.csv.gz \
  --current /path/to/approved/recent-features.csv.gz \
  --features amount payment_term --output /tmp/invoiceiq-drift-report.json
```

This needs a real, schema-compatible recent extract; comparing training data against itself proves only that the script runs.
