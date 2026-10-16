import logging
import asyncio
import os
from contextlib import asynccontextmanager

from app.core.scoped_reads import ScopedFinancialReadMiddleware
from fastapi import FastAPI, Request

from app.core.observability import configure_langsmith, configure_logging

configure_logging()
configure_langsmith()

from app.api.companies import router as companies_router  # noqa: E402
from app.api.dashboard import router as dashboard_router  # noqa: E402
from app.api.health import router as health_router  # noqa: E402
from app.api.invoices import router as invoices_router  # noqa: E402
from app.api.ml_intelligence import router as ml_intelligence_router  # noqa: E402
from app.api.recovery_cases import router as recovery_cases_router  # noqa: E402
from app.api.voice import router as voice_router  # noqa: E402
from app.api.auth import router as auth_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.observability import RequestContextMiddleware  # noqa: E402
from app.core.metrics import MetricsMiddleware, metrics_response  # noqa: E402
from app.core.mutation_auth import HostedMutationAuthMiddleware  # noqa: E402
from app.core.request_limits import RequestBodyLimitMiddleware  # noqa: E402
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
        extra={"provider": provider, "model": model, "llm_called": False},
    )
    public = os.getenv("PUBLIC_READ_ONLY", "").lower() == "true"
    if os.getenv("SAMPLE_SYNTHETIC", "").lower() == "true" and not public:
        raise RuntimeError("Synthetic sample requires PUBLIC_READ_ONLY=true")
    if not public:
        start_scheduler()
    dispatcher = None
    if settings.outbox_dispatch_enabled and not public:
        from app.core.db import async_session_factory
        from app.events.outbox import dispatch_loop
        dispatcher = asyncio.create_task(dispatch_loop(async_session_factory, get_publisher()), name="event-outbox")
    try:
        yield
    finally:
        try:
            if dispatcher is not None:
                dispatcher.cancel()
                try:
                    await dispatcher
                except asyncio.CancelledError:
                    pass
            await stop_scheduler()
            await get_publisher().close()
        finally:
            from app.core.db import engine
            from app.services.inference_telemetry import dispose_telemetry_engine

            try:
                await dispose_telemetry_engine()
            finally:
                await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
from app.core.portal_guard import PortalLegacyGuard  # noqa: E402
app.add_middleware(PortalLegacyGuard)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(MetricsMiddleware)
app.add_middleware(HostedMutationAuthMiddleware)
app.add_middleware(ScopedFinancialReadMiddleware)
app.add_middleware(RequestBodyLimitMiddleware)

from app.api.public_analytics import router as public_analytics_router  # noqa: E402
from app.core.public_read_only import PublicReadOnlyMiddleware  # noqa: E402
app.add_middleware(PublicReadOnlyMiddleware)
app.include_router(public_analytics_router)

app.include_router(health_router)
app.include_router(companies_router)
app.include_router(invoices_router)
app.include_router(ml_intelligence_router)
app.include_router(recovery_cases_router)
app.include_router(voice_router)
app.include_router(auth_router)
from app.api.portal import router as portal_router  # noqa: E402
app.include_router(portal_router)
app.include_router(dashboard_router)


@app.get("/internal/metrics", include_in_schema=False)
async def private_metrics(request: Request):
    return metrics_response(request)


from app.api.preserved_dashboard import router as preserved_dashboard_router  # noqa: E402
app.include_router(preserved_dashboard_router)
from app.api.workspace_views import router as workspace_views_router  # noqa: E402
app.include_router(workspace_views_router)
