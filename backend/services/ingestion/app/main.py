from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI
from pydantic import BaseModel
from redis.asyncio import Redis

from app.api.alerts import router as alerts_router
from app.core.apps import AppSource, AppStore
from app.core.config import Settings
from app.core.ratelimit import RateLimiter
from app.outbox.writer import Outbox, PostgresOutbox
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "ingestion"

logger = logging.getLogger(__name__)

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if postgres:
            await postgres.start()
        try:
            yield
        finally:
            if postgres:
                await postgres.stop()
            if registry:
                await registry.aclose()
            if redis:
                await redis.aclose()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.apps = AppStore(apps, settings.apps_dir)
    app.state.outbox = outbox or postgres
    app.state.rate_limiter = rate_limiter
    app.include_router(alerts_router)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    # Readiness probe (Kubernetes, docs/plan.md Day 18). For now it is the
    # same as /healthz; Day 19 makes it check what this service needs.
    @app.get("/readyz", response_model=HealthResponse)
    async def readyz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    return app


app = create_app()
