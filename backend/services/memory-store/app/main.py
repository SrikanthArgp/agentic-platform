"""memory-store: behavioural context per alert_key (ARCHITECTURE §6, ADR-0016/0017/0018).

gRPC `GetContext` on GRPC_PORT (50051), fed by an `alert.decided`
consumer. HTTP serves only `/healthz`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.service import MemoryService
from app.db.postgres import PostgresEventStore, create_pool
from app.grpc.server import start_grpc_server
from app.kafka.decisions import DecisionConsumer
from app.redis.cache import MemoryCache
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "memory-store"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    pool = await create_pool(settings.postgres_dsn)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    registry = RegistryClient(settings.registry_url, ttl_s=settings.manifest_ttl_s)
    service = MemoryService(PostgresEventStore(pool), MemoryCache(redis), registry)
    grpc_server = await start_grpc_server(service, settings.grpc_port)
    consumer = DecisionConsumer(service, settings.kafka_bootstrap_servers) if settings.kafka_enabled else None
    if consumer:
        consumer.start()
    try:
        yield
    finally:
        if consumer:
            await consumer.stop()
        await grpc_server.stop(grace=5)
        await registry.aclose()
        await redis.aclose()
        await pool.close()


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)
