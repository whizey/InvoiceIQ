# Contributing

## Set up
- Backend: Python 3.12+, `cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- Frontend: Node 22, `cd frontend && npm ci`.
- Tests use a **disposable** PostgreSQL, never a developer database. See `backend/docs/BACKEND_TEST_RUNNER.md`.

## Before you open a pull request
```sh
cd backend && ruff check app tests && python -m pytest -q tests
cd frontend && npm run lint && npm run typecheck && npm test && npm run build
```
CI runs the same checks plus a coverage gate (80%), secret scanning and dependency audits.

## Conventions
- Commit messages follow `type(scope): summary` (`feat`, `fix`, `docs`, `test`, `chore`, `ci`, `perf`, `refactor`).
- Migrations are Alembic revisions and must apply up, down and up again.
- Every tenant-scoped query filters by `workspace_id`; add a cross-tenant test for new endpoints.
- Decisions that change behaviour belong in a short note under `docs/adr/`.
- Never commit credentials, `.env` files or personal tooling state; `scripts/verification/check_repo_privacy.py` enforces this.
