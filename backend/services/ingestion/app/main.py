from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.alerts import router as alerts_router
from app.core.apps import FileAppStore
from app.core.config import Settings
from app.kafka.decisions import DecisionConsumer, DecisionTracker
from app.kafka.publisher import KafkaPublisher, Publisher
from observability import setup_observability

SERVICE_NAME = "ingestion"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def create_app(settings: Settings | None = None, publisher: Publisher | None = None) -> FastAPI:
    """`publisher` is injected in tests; otherwise a Kafka producer is started with the app."""
    settings = settings or Settings.from_env()
    kafka = KafkaPublisher(settings.kafka_bootstrap_servers) if publisher is None and settings.kafka_enabled else None
    tracker = DecisionTracker()
    decisions = DecisionConsumer(tracker, settings.kafka_bootstrap_servers) if kafka else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if kafka:
            kafka.start()
        if decisions:
            decisions.start()
        try:
            yield
        finally:
            if decisions:
                await decisions.stop()
            if kafka:
                await kafka.stop()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.settings = settings
    app.state.apps = FileAppStore(settings.apps_dir)
    app.state.publisher = publisher or kafka
    app.state.tracker = tracker
    app.include_router(alerts_router)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    return app


app = create_app()
