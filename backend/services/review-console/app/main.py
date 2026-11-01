"""review-console: cases for escalated alerts, and analyst verdicts (ARCHITECTURE §4 step 4).

Consumes `alert.decided` (only `ESCALATE` becomes a case), serves the
analyst REST API, and writes each verdict to `verdict_outbox`, which the
`outbox-relay` worker publishes as `verdict.recorded` (ADR-0024, ADR-0030).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.cases import router as cases_router
from app.core.config import Settings
from app.core.service import CaseService, CaseStore
from app.kafka.decisions import DecisionConsumer
from observability import setup_observability

SERVICE_NAME = "review-console"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def create_app(settings: Settings | None = None, store: CaseStore | None = None) -> FastAPI:
    """`store` is injected in tests; otherwise a Postgres pool and (unless
    KAFKA_ENABLED=false) the `alert.decided` consumer start with the app."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if store is not None:
            app.state.service = CaseService(store)
            yield
            return
        from app.db.postgres import PostgresCaseStore, create_pool

        pool = await create_pool(settings.postgres_dsn)
        service = app.state.service = CaseService(PostgresCaseStore(pool))
        consumer = DecisionConsumer(service, settings.kafka_bootstrap_servers) if settings.kafka_enabled else None
        if consumer:
            consumer.start()
        try:
            yield
        finally:
            if consumer:
                await consumer.stop()
            await pool.close()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.include_router(cases_router)

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
