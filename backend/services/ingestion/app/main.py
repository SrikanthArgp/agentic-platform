from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis

from app.api.alerts import router as alerts_router
from app.core.apps import AppSource, AppStore
from app.core.config import Settings
from app.core.ratelimit import RateLimiter
from app.outbox.writer import Outbox, PostgresOutbox
from lifecycle import Lifecycle
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "ingestion"

logger = logging.getLogger(__name__)

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


class NotReadyResponse(HealthResponse):
    reasons: list[str]


def create_app(
    settings: Settings | None = None,
    outbox: Outbox | None = None,
    apps: AppSource | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """`outbox`, `apps` (the manifest source) and `rate_limiter` are
    injected in tests; otherwise the Postgres outbox, a `RegistryClient`,
    and a Redis-backed limiter when `REDIS_URL` is set."""
    settings = settings or Settings.from_env()
    redis = None
    if rate_limiter is None and settings.redis_url:
        # Short timeouts: a Redis outage must not slow intake down (docs/adr/0029).
        redis = Redis.from_url(settings.redis_url, socket_timeout=0.2, socket_connect_timeout=0.2)
        rate_limiter = RateLimiter(
            redis,
            default_per_minute=settings.rate_limit_per_minute,
            default_per_source_per_minute=settings.rate_limit_per_source_per_minute,
        )
    elif rate_limiter is None:
        logger.warning("REDIS_URL is not set: events are not rate limited")
    registry = None
    if apps is None:
        apps = registry = RegistryClient(settings.registry_url, ttl_s=settings.manifest_ttl_s)
    postgres = PostgresOutbox(settings.postgres_dsn) if outbox is None else None
    outbox = outbox or postgres
    life = Lifecycle(SERVICE_NAME)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if postgres:
            await postgres.start()
        # SIGTERM: /readyz fails at once; uvicorn then lets in-flight
        # requests (each at most one outbox write, WRITE_TIMEOUT_S) finish
        # before this lifespan's shutdown runs (docs/plan.md Day 19).
        life.install_sigterm_hook()
        life.mark_started()
        try:
            yield
        finally:
            life.begin_drain()
            if postgres:
                await postgres.stop()
            if registry:
                await registry.aclose()
            if redis:
                await redis.aclose()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.apps = AppStore(apps, settings.apps_dir)
    app.state.outbox = outbox
    app.state.lifecycle = life
    app.state.rate_limiter = rate_limiter
    app.include_router(alerts_router)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    # Readiness (docs/plan.md Day 19): started, not draining, and the
    # outbox's Postgres answers, i.e. a `202` is possible right now.
    # Liveness (/healthz) never checks dependencies: a Postgres outage must
    # take pods out of rotation, not restart them.
    @app.get("/readyz", response_model=HealthResponse, responses={503: {"model": NotReadyResponse}})
    async def readyz():
        reasons = await life.not_ready_reasons({"outbox": outbox.ping})
        if reasons:
            body = NotReadyResponse(status="unavailable", service=SERVICE_NAME, reasons=reasons)
            return JSONResponse(body.model_dump(), status_code=503)
        return HealthResponse(status="ok", service=SERVICE_NAME)

    return app


app = create_app()
