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
from app.kafka.publisher import KafkaPublisher, Publisher
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
    publisher: Publisher | None = None,
    apps: AppSource | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """`publisher`, `apps` (the manifest source) and `rate_limiter` are
    injected in tests; otherwise a Kafka producer, a `RegistryClient`, and a
    Redis-backed limiter when `REDIS_URL` is set."""
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
    kafka = KafkaPublisher(settings.kafka_bootstrap_servers) if publisher is None and settings.kafka_enabled else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if kafka:
            kafka.start()
        try:
            yield
        finally:
            if kafka:
                await kafka.stop()
            if registry:
                await registry.aclose()
            if redis:
                await redis.aclose()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.apps = AppStore(apps, settings.apps_dir)
    app.state.publisher = publisher or kafka
    app.state.rate_limiter = rate_limiter
    app.include_router(alerts_router)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    return app


app = create_app()
