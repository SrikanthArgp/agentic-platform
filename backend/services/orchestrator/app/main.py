import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
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
from lifecycle import Lifecycle
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "orchestrator"

setup_observability(SERVICE_NAME)


# Readiness probe budget per dependency (the probe itself times out at 3s).
DEPENDENCY_TIMEOUT_S = 1.0
# On top of RUN_TIMEOUT_S: publishing and committing the last alert.
DRAIN_SLACK_S = 15.0


class HealthResponse(BaseModel):
    status: str
    service: str


class NotReadyResponse(HealthResponse):
    reasons: list[str]


def base_url(url: str) -> str:
    """`http://tool-gateway:8000/mcp` -> `http://tool-gateway:8000`."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def http_check(http: httpx.AsyncClient, url: str):
    async def check() -> str | None:
        try:
            response = await http.get(url, timeout=DEPENDENCY_TIMEOUT_S)
        except httpx.HTTPError as e:
            return f"unreachable ({type(e).__name__})"
        return None if response.status_code == 200 else f"HTTP {response.status_code}"

    return check


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
        run_timeout_s=settings.run_timeout_s,
    )


life = Lifecycle(SERVICE_NAME)


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
    probe = httpx.AsyncClient()
    app.state.readiness_checks = readiness_checks(settings, pipeline, probe)
    # SIGTERM: /readyz fails at once; once uvicorn has closed HTTP, the
    # shutdown below lets the current alert and any gRPC RunAgent finish
    # (each bounded by RUN_TIMEOUT_S) before the clients close (Day 19).
    life.install_sigterm_hook()
    life.mark_started()
    try:
        yield
    finally:
        life.begin_drain()
        drain_s = settings.run_timeout_s + DRAIN_SLACK_S
        await asyncio.gather(
            pipeline.stop(drain_s) if pipeline else asyncio.sleep(0),
            grpc_server.stop(grace=drain_s),
        )
        await probe.aclose()
        await memory.aclose()
        await registry.aclose()
        await redis.aclose()


def readiness_checks(settings: Settings, pipeline: AlertPipeline | None, http: httpx.AsyncClient) -> dict:
    """Ready = this pod can decide an alert now: its Kafka consumer has
    joined the group (when Kafka is on), and `registry` and `tool-gateway`
    answer `/healthz`. Liveness (/healthz) checks none of this, so an
    outage takes pods out of rotation without restarting them."""
    checks = {}
    if pipeline is not None:
        async def kafka() -> str | None:
            return None if pipeline.ready else "consumer hasn't joined its group"

        checks["kafka"] = kafka
    checks["registry"] = http_check(http, base_url(settings.registry_url) + "/healthz")
    checks["tool-gateway"] = http_check(http, base_url(settings.tool_gateway_url) + "/healthz")
    return checks


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
app.state.readiness_checks = {}


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)


# Readiness (docs/plan.md Day 19): started, not draining, and the checks in
# readiness_checks() pass.
@app.get("/readyz", response_model=HealthResponse, responses={503: {"model": NotReadyResponse}})
async def readyz():
    reasons = await life.not_ready_reasons(app.state.readiness_checks)
    if reasons:
        body = NotReadyResponse(status="unavailable", service=SERVICE_NAME, reasons=reasons)
        return JSONResponse(body.model_dump(), status_code=503)
    return HealthResponse(status="ok", service=SERVICE_NAME)
