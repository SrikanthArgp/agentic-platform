"""outbox-relay: drains `outbox` and `verdict_outbox` to Kafka (ADR-0030).

A worker, not a platform service: no API beyond `/healthz` (the process
is up) and `/readyz` (it has a Postgres session: `active` holds the relay
lock and drains, `standby` waits for it). One container in Compose, one
Deployment with `replicas: 1` on kind; an overlapping instance during a
rollout just waits as `standby`.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings
from app.kafka import KafkaProducer
from app.postgres import PostgresDatabase
from app.relay import Relay, Role
from observability import setup_observability

SERVICE_NAME = "outbox-relay"

logger = logging.getLogger(__name__)

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


class ReadyResponse(BaseModel):
    status: str
    role: Role
    # The producer has connected once; aiokafka reconnects on its own after.
    kafka_producer_started: bool


def create_app(settings: Settings | None = None, relay: Relay | None = None, start: bool = True) -> FastAPI:
    """`relay` is injected in tests; `start=False` serves the endpoints
    without running the loop."""
    settings = settings or Settings.from_env()
    producer = KafkaProducer(settings.kafka_bootstrap_servers) if relay is None else None
    if relay is None:
        relay = Relay(
            PostgresDatabase(settings.postgres_dsn, settings.lock_key),
            producer,
            batch_size=settings.batch_size,
            poll_interval_s=settings.poll_interval_s,
            retention_h=settings.retention_h,
            prune_interval_s=settings.prune_interval_s,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = None
        if producer:
            producer.start()
        if start:
            task = asyncio.create_task(relay.run(), name="outbox-relay")
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if producer:
                await producer.stop()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE_NAME)

    @app.get("/readyz", response_model=ReadyResponse)
    async def readyz() -> JSONResponse:
        ready = relay.role in (Role.ACTIVE, Role.STANDBY)
        body = ReadyResponse(
            status="ok" if ready else "unavailable",
            role=relay.role,
            kafka_producer_started=producer.connected if producer else True,
        )
        return JSONResponse(body.model_dump(mode="json"), status_code=200 if ready else 503)

    return app


app = create_app()
