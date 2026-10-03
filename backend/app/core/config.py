from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve against this file, not process cwd — uvicorn --reload workers
# (and tests) do not always share the same working directory as run.sh.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ENV_FILE = _BACKEND_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8")

    app_name: str = "ai-revenue-recovery-agent"
    env: str = "local"
    database_url: str = (
        "postgresql+asyncpg://recovery_user:recovery_pass@localhost:5432/recovery_db"
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _use_asyncpg_driver(cls, value: object) -> object:
        """Rewrite a provider's connection string to use the asyncpg driver.

        Render, Heroku and Railway all hand out their managed Postgres URL
        as plain postgresql:// (Heroku historically postgres://), but
        create_async_engine in app/core/db.py requires the +asyncpg driver
        in the scheme. Without this rewrite, pasting a provider's
        DATABASE_URL in verbatim crashes the API at import with
        SQLAlchemy's "the asyncio extension requires an async driver"
        rather than working -- a deployment blocker whose error message
        points nowhere near the cause.
        """
        if isinstance(value, str):
            if value.startswith("postgresql://"):
                return "postgresql+asyncpg://" + value[len("postgresql://"):]
            if value.startswith("postgres://"):
                return "postgresql+asyncpg://" + value[len("postgres://"):]
        return value

    # LLM provider: if unset, diagnosis/intervention fall back to a deterministic
    # rule-based agent (see app/agents/llm_client.py) — no key required to run the app.
    # Gemini (GOOGLE_API_KEY) is preferred when both keys are present.
    google_api_key: str | None = None
    openai_api_key: str | None = None
    # Optional override. If omitted, Gemini uses gemini-2.5-flash and OpenAI uses gpt-4o-mini.
    llm_model: str | None = None

    # Event publishing: if unset, domain events are logged instead of
    # published (see app/events/) — no Kafka required to run the app.
    kafka_bootstrap_servers: str | None = None

    # Locking/idempotency: if unset, falls back to an in-process lock
    # (see app/core/locks.py) — no Redis required to run the app, but the
    # fallback only guards against races within this one process.
    redis_url: str | None = None

    # Observability: structured (JSON) logs are always on (see
    # app/core/observability.py). LangSmith tracing of the LangGraph
    # workflow is opt-in — off by default, no account required to run the app.
    log_level: str = "INFO"
    langchain_tracing_v2: bool = False
    langchain_api_key: str | None = None
    langchain_project: str = "ai-revenue-recovery-agent"

    # In-process recovery loop (detect overdue + one cycle per active case).
    # Off by default so CI/tests stay deterministic; enable in backend/.env.
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 60
    scheduler_initial_delay_seconds: int = 5

    # Real-reminder-email demo (see app/tools/email_provider.py). The ONLY
    # allowed destination for a real send is this address — never a seeded
    # contact's @example.com. If unset, the send-reminder endpoint errors
    # rather than silently falling back to a fake address.
    demo_notify_email: str | None = None
    # If unset, the endpoint still runs (policy still gates it) but records
    # a SIMULATED CommunicationLog instead of calling Resend.
    resend_api_key: str | None = None
    resend_from: str | None = None

    # Hinglish voice-call demo (see app/tools/sarvam_client.py). If unset,
    # calls run as a fully simulated (text-only, no audio) turn sequence.
    sarvam_api_key: str | None = None

    @field_validator(
        "google_api_key",
        "openai_api_key",
        "llm_model",
        "demo_notify_email",
        "resend_api_key",
        "resend_from",
        "sarvam_api_key",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


settings = Settings()
