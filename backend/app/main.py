import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.observability import configure_langsmith, configure_logging

configure_logging()
configure_langsmith()

from app.api.companies import router as companies_router  # noqa: E402
from app.api.dashboard import router as dashboard_router  # noqa: E402
from app.api.health import router as health_router  # noqa: E402
from app.api.invoices import router as invoices_router  # noqa: E402
from app.api.recovery_cases import router as recovery_cases_router  # noqa: E402
from app.api.voice import router as voice_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.observability import RequestContextMiddleware  # noqa: E402
from app.events import get_publisher  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.agents.llm_client import configured_llm
    from app.services.scheduler import start_scheduler, stop_scheduler

    provider, model = configured_llm()
    logging.getLogger("app.llm").info(
        "llm startup provider=%s model=%s",
        provider,
        model,
        extra={"provider": provider, "model": model, "llm_called": provider != "fallback"},
    )
    start_scheduler()
    yield
    await stop_scheduler()
    await get_publisher().close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)

app.include_router(health_router)
app.include_router(companies_router)
app.include_router(invoices_router)
app.include_router(recovery_cases_router)
app.include_router(voice_router)
app.include_router(dashboard_router)
