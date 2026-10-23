# Tech stack

Exact versions are pinned in `backend/requirements.txt` and `frontend/package.json`; this is what each piece is for.

## Backend

Python 3.12, FastAPI and Uvicorn, SQLAlchemy 2 (async) with asyncpg, Pydantic for request models and
settings, Alembic for migrations, httpx for outbound calls and API tests. openpyxl reads Excel imports.

## Data

PostgreSQL 16 on Supabase. Redis is optional (a distributed lock for the older workflow). Model files are
stored in Git LFS.

## Models

LightGBM for the late-payment model, with scikit-learn, XGBoost and scikit-survival used in training and
comparison, SHAP for explanations, pandas and NumPy throughout. The cash forecast's Kaplan-Meier curve is written
out by hand in `services/cash_forecast.py` rather than pulled from a library.

## Language models

Gemini (via LangChain) with Groq as a fallback, for the assistant and reminder drafts. Gemini
embeddings for note search. LangGraph drives the older recovery workflow. LangSmith tracing is optional.

## Events

A transactional outbox in PostgreSQL with an in-process worker. Kafka 3.9 (aiokafka) is optional; the
hosted Space runs a single node, and Redpanda is used for local testing.

## Frontend

Next.js 16 (App Router), React 19, TypeScript, ESLint. Charts are plain SVG.

## Testing and CI

pytest with an 80% coverage gate, the Node test runner, Playwright, Ruff, gitleaks, pip-audit,
npm audit and Dependabot, run in GitHub Actions.

## Hosting

Vercel for the site, a Hugging Face Space for the API, Supabase for the database. Sentry is optional.
