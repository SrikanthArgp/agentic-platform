from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from redis.asyncio import Redis

from app.agent.llm import build_chat_model
from app.agent.loop import AgentRunner
from app.core.config import Settings
from app.core.manifest import ManifestStore
from app.governance.budget import TokenBudget
from app.grpc.server import start_grpc_server
from app.kafka.alerts import AlertPipeline
from app.kafka.dedupe import SeenAlerts
from app.memory.client import MemoryStoreClient
from app.tools.gateway import MCPToolGateway
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "orchestrator"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def build_runner(
    settings: Settings, registry: RegistryClient, memory: MemoryStoreClient, budget: TokenBudget | None = None
) -> AgentRunner:
    return AgentRunner(
        manifests=ManifestStore(registry, settings.apps_dir),
        gateway=MCPToolGateway(settings.tool_gateway_url, settings.tool_call_timeout_s),
        model=build_chat_model(settings.llm_provider, settings.llm_model),
        memory=memory,
        max_tool_rounds=settings.max_tool_rounds,
        min_confidence=settings.min_confidence,
        callable_timeout_s=settings.callable_timeout_s,
        budget=budget,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Built here, not at import: the LLM client needs its API key, and tests
    # import this module without one.
    settings = Settings.from_env()
    registry = RegistryClient(settings.registry_url, ttl_s=settings.manifest_ttl_s)
    # One channel for the process's lifetime, reused by every run (docs/adr/0019).
    memory = MemoryStoreClient(settings.memory_store_target, settings.memory_store_timeout_s)
    # Short timeouts: a budget check must never be what makes a run slow (docs/adr/0029).
    redis = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.5
    )
    budget = TokenBudget(redis, default_daily_tokens=settings.daily_token_budget)
    runner = build_runner(settings, registry, memory, budget)
    grpc_server = await start_grpc_server(runner, settings.grpc_port)
    seen = SeenAlerts(redis, ttl_s=settings.seen_ttl_s)
    pipeline = AlertPipeline(runner, settings.kafka_bootstrap_servers, seen=seen) if settings.kafka_enabled else None
    if pipeline:
        pipeline.start()
    try:
        yield
    finally:
        if pipeline:
            await pipeline.stop()
        await grpc_server.stop(grace=5)
        await memory.aclose()
        await registry.aclose()
        await redis.aclose()


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)

# Readiness probe (Kubernetes, docs/plan.md Day 18). For now it is the
# same as /healthz; Day 19 makes it check what this service needs.
@app.get("/readyz", response_model=HealthResponse)
async def readyz() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)
