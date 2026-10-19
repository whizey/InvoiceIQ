# Technology stack

Every tool used in the project, grouped by purpose, with the version pinned in the repository and why it was chosen.

## Languages

| Language | Used for |
|---|---|
| Python 3.12+ | Backend, ML, agents, scripts, tests |
| TypeScript / JavaScript | Next.js application, components, browser tests, the reference dashboard scripts |
| SQL (PostgreSQL dialect) | Migrations, JSONB columns, upserts with `ON CONFLICT` |
| Bash | `run.sh`, packaging and CI helper scripts |
| YAML / Docker | CI workflows, Compose files, container builds |

## Backend

| Tool | Version | Role and reason |
|---|---|---|
| FastAPI | 0.141.1 | HTTP API with dependency injection (used for tenant identity) and automatic validation |
| Uvicorn | 0.52.4 | ASGI server |
| SQLAlchemy (asyncio) | 2.0.52 | ORM and queries, fully async |
| asyncpg | 0.31.0 | PostgreSQL driver |
| Pydantic | 2.13.5 (2.12.5 in the Hugging Face Space) | Request and response models, settings |
| pydantic-settings | 2.15.0 | Typed environment configuration |
| Alembic | 1.19.1 | Database migrations (20 revisions) |
| httpx | 0.28.1 | Outbound HTTP and in-process API tests |
| python-multipart | 0.0.31 | Audio upload for transcription |
| python-dotenv | 1.2.2 | Local environment files |
| prometheus-client | 0.23.1 | Metrics |

## Machine learning and data

| Tool | Version | Role |
|---|---|---|
| LightGBM | 4.6.0 | The late-payment champion model |
| XGBoost | 3.4.1 (`xgboost-cpu` in the Space) | Benchmarks, the earlier recovery model and the AFT survival challenger |
| scikit-learn | 1.9.0 | Preprocessing, calibration, metrics |
| scikit-survival | 0.28.0 | Survival models (Cox-style) for purchase-to-pay clearance |
| SHAP | 0.52.0 | TreeSHAP explanations of individual scores |
| pandas / NumPy | 3.0.5 / 2.5.2 | Feature engineering |

## AI and agents

| Tool | Version | Role |
|---|---|---|
| LangGraph | 1.2.11 | The recovery workflow as an explicit state graph |
| langchain-core | 1.6.1 | Message and model abstractions |
| langchain-google-genai | 4.3.6 | Google Gemini client |
| langchain-openai | 1.6.0 | Optional alternative provider |
| Google Gemini | `gemini-3.6-flash`, fast path `gemini-3.5-flash-lite` | The AI assistant, voice-turn reasoning and audio transcription |
| Sarvam | n/a (REST) | Speech-to-text and text-to-speech for the Hinglish voice-call sample. Speech only; it never decides actions |
| Browser Web Speech API + MediaRecorder | n/a | Voice input and read-aloud in the assistant, with server transcription as a fallback |
| LangSmith | optional | Optional tracing of workflow runs, off by default |

## Events and messaging

| Tool | Version | Role |
|---|---|---|
| aiokafka | 0.14.0 | Kafka producer and consumer, with TLS and SASL options |
| Apache Kafka | 3.9.0 (KRaft, single node) | The hosted broker, started beside the API in the Space |
| Redpanda (Docker) | v24.3.7 broker, v2.8.2 console | Local Kafka-compatible broker and web console for trying the pipeline |
| Java runtime | default-jre-headless | Runs the embedded Kafka |
| Transactional outbox (custom) | n/a | Durable, at-least-once event hand-off |
| Redis | 8.1.0 client | Optional distributed lock; in-process lock is the fallback |
| Resend | REST | Optional real reminder email to a configured sample address |

## Frontend

| Tool | Version | Role |
|---|---|---|
| Next.js | 16.4 (App Router, webpack build) | Pages, server components and the API proxy |
| React | 19.2.8 | UI |
| TypeScript | 5 | Typed components and contracts |
| ESLint | 9 (`eslint-config-next`) | Linting |
| Playwright | 1.64 | Browser end-to-end journeys |
| Node test runner | built in | Unit and contract tests |

## Data stores

| Tool | Role |
|---|---|
| PostgreSQL 16 | System of record, JSONB, row-level scoping by workspace |
| Supabase (session pooler) | Hosted PostgreSQL for the live site |
| Git LFS | Stores the versioned model artifacts (21 large files) |

## Delivery and infrastructure

| Tool | Role |
|---|---|
| Docker and Docker Compose | Local database, staging rehearsal, backend and frontend images |
| GitHub Actions | CI, security scanning and the gated deploy |
| Vercel | Hosts the Next.js application |
| Hugging Face Spaces (Gradio SDK shell) | Hosts the FastAPI API; a small Gradio placeholder satisfies the platform's requirements while uvicorn serves the API |
| Supabase | Hosted PostgreSQL |
| Vercel CLI, `huggingface` git remote | Used by the deploy workflow |

## Quality and security tooling

| Tool | Role |
|---|---|
| pytest, pytest-asyncio, coverage | Backend tests with an 80% coverage gate |
| Ruff | Python lint |
| gitleaks | Secret scanning of the repository history |
| `npm audit` and a Python dependency audit | Known-vulnerability checks |
| Custom privacy check | Blocks private local files and credentials from the repository |

## Why these choices

- **FastAPI plus async SQLAlchemy** gives typed, validated endpoints and non-blocking database access with little code.
- **LightGBM** suits tabular, behaviour-heavy features and works with TreeSHAP for per-invoice explanations.
- **LangGraph** makes the workflow an auditable graph with explicit nodes and a conditional "hold" branch, rather than an opaque prompt chain.
- **Postgres plus an outbox** keeps the system of record and the event log consistent without a distributed transaction.
- **Kafka-compatible events** let the same code run against a local Redpanda, a hosted broker, or no broker at all.
- **Free-tier friendly hosting** (Vercel, Hugging Face, Supabase) keeps the live product runnable at no cost; the trade-offs are listed in [CICD_AND_DEPLOYMENT.md](CICD_AND_DEPLOYMENT.md).
