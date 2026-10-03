from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.agent.llm import LLMClient
from app.agent.loop import AgentRunner
from app.core.config import Settings
from app.core.manifest import FileManifestStore
from app.grpc.server import start_grpc_server
from app.kafka.alerts import AlertPipeline
from app.tools.gateway import MCPToolGateway
from observability import setup_observability

SERVICE_NAME = "orchestrator"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "openai":
        from app.agent.openai_llm import OpenAIChatClient

        return OpenAIChatClient(settings.llm_model)
    raise ValueError(f"Unsupported LLM_PROVIDER '{settings.llm_provider}'.")


def build_runner(settings: Settings) -> AgentRunner:
    return AgentRunner(
        manifests=FileManifestStore(settings.apps_dir),
        gateway=MCPToolGateway(settings.tool_gateway_url),
        llm=build_llm(settings),
        max_tool_rounds=settings.max_tool_rounds,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Built here, not at import: the LLM client needs its API key, and tests
    # import this module without one.
    settings = Settings.from_env()
    runner = build_runner(settings)
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


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)
