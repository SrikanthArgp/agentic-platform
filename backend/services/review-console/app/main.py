"""review-console: cases for escalated alerts, and analyst verdicts (ARCHITECTURE §4 step 4).

Consumes `alert.decided` (only `ESCALATE` becomes a case), serves the
analyst REST API, and publishes `verdict.recorded` for `memory-store`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.cases import router as cases_router
from app.core.config import Settings
from app.core.service import CaseService, CaseStore
from app.kafka.decisions import DecisionConsumer
from app.kafka.publisher import KafkaPublisher, PublishError, Publisher
from observability import setup_observability

SERVICE_NAME = "review-console"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


class _NoKafka:
    """With KAFKA_ENABLED=false: cases are readable, verdicts get a 503."""

    async def publish(self, topic: str, key: bytes, value: bytes) -> None:
        raise PublishError("Kafka is disabled (KAFKA_ENABLED=false).")


def create_app(
    settings: Settings | None = None, store: CaseStore | None = None, publisher: Publisher | None = None
) -> FastAPI:
    """`store` and `publisher` are injected in tests; otherwise a Postgres pool,
    a Kafka producer and the `alert.decided` consumer start with the app."""
    settings = settings or Settings.from_env()
    kafka = KafkaPublisher(settings.kafka_bootstrap_servers) if publisher is None and settings.kafka_enabled else None
    publisher = publisher or kafka or _NoKafka()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if store is not None:
            app.state.service = CaseService(store, publisher)
            yield
            return
        from app.db.postgres import PostgresCaseStore, create_pool

        pool = await create_pool(settings.postgres_dsn)
        service = app.state.service = CaseService(PostgresCaseStore(pool), publisher)
        consumer = DecisionConsumer(service, settings.kafka_bootstrap_servers) if kafka else None
        if kafka:
            kafka.start()
        if consumer:
            consumer.start()
        try:
            yield
        finally:
            if consumer:
                await consumer.stop()
            if kafka:
                await kafka.stop()
            await pool.close()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.include_router(cases_router)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    return app


app = create_app()
