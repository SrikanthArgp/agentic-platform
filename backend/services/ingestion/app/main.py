from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.alerts import router as alerts_router
from app.core.apps import AppSource, AppStore
from app.core.config import Settings
from app.kafka.publisher import KafkaPublisher, Publisher
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "ingestion"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def create_app(
    settings: Settings | None = None, publisher: Publisher | None = None, apps: AppSource | None = None
) -> FastAPI:
    """`publisher` and `apps` (the manifest source) are injected in tests;
    otherwise a Kafka producer and a `RegistryClient` are used."""
    settings = settings or Settings.from_env()
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

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.apps = AppStore(apps, settings.apps_dir)
    app.state.publisher = publisher or kafka
    app.include_router(alerts_router)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    return app


app = create_app()
