from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.agent.llm import build_chat_model
from app.agent.loop import AgentRunner
from app.core.config import Settings
from app.core.manifest import ManifestStore
from app.grpc.server import start_grpc_server
from app.kafka.alerts import AlertPipeline
from app.memory.client import MemoryStoreClient
from app.tools.gateway import MCPToolGateway
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "orchestrator"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def build_runner(settings: Settings, registry: RegistryClient, memory: MemoryStoreClient) -> AgentRunner:
    return AgentRunner(
        manifests=ManifestStore(registry, settings.apps_dir),
        gateway=MCPToolGateway(settings.tool_gateway_url),
        model=build_chat_model(settings.llm_provider, settings.llm_model),
        memory=memory,
        max_tool_rounds=settings.max_tool_rounds,
        min_confidence=settings.min_confidence,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Built here, not at import: the LLM client needs its API key, and tests
    # import this module without one.
    settings = Settings.from_env()
    registry = RegistryClient(settings.registry_url, ttl_s=settings.manifest_ttl_s)
    # One channel for the process's lifetime, reused by every run (docs/adr/0019).
    memory = MemoryStoreClient(settings.memory_store_target, settings.memory_store_timeout_s)
    runner = build_runner(settings, registry, memory)
    grpc_server = await start_grpc_server(runner, settings.grpc_port)
    pipeline = AlertPipeline(runner, settings.kafka_bootstrap_servers) if settings.kafka_enabled else None
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


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)
