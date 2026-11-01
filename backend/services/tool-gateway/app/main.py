import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.routing import Route

from app.core.faults import apply_faults
from app.core.loader import load_app_tools
from app.core.registry import ToolRegistry
from app.core.resilience import ResiliencePolicy, ToolRunner
from app.mcp.server import AppSource, build_mcp_server
from app.tools import similar_past_cases
from app.tools.similar_past_cases import CaseSource, ReviewConsoleCases
from observability import setup_observability
from registry_client import RegistryClient

SERVICE_NAME = "tool-gateway"
MCP_PATH = "/mcp"

# backend/services/tool-gateway/app/main.py -> backend/apps; the Dockerfile
# keeps the same layout under /repo/backend, so this holds in both places.
DEFAULT_APPS_DIR = Path(__file__).resolve().parents[3] / "apps"
# Host headers the MCP endpoint accepts (DNS-rebinding protection): host
# access via the published port, and the Compose service name.
DEFAULT_ALLOWED_HOSTS = "localhost:*,127.0.0.1:*,tool-gateway:*"
DEFAULT_REGISTRY_URL = "http://localhost:8005"
# similar-past-case-lookup reads past cases through review-console's API.
DEFAULT_REVIEW_CONSOLE_URL = "http://localhost:8006"

setup_observability(SERVICE_NAME)


class HealthResponse(BaseModel):
    status: str
    service: str


def create_app(
    apps_dir: Path | None = None,
    allowed_hosts: list[str] | None = None,
    apps: AppSource | None = None,
    cases: CaseSource | None = None,
) -> FastAPI:
    """`apps` (the source of each agent's allowlist) and `cases` (what
    `similar-past-case-lookup` reads) are injected in tests; otherwise a
    `RegistryClient` with the platform's manifest TTL and `review-console`."""
    apps_dir = apps_dir or Path(os.environ.get("APPS_DIR", DEFAULT_APPS_DIR))
    if allowed_hosts is None:
        allowed_hosts = os.environ.get("MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS).split(",")

    registry = ToolRegistry()
    load_app_tools(apps_dir, registry)
    # Global tools: platform code, loaded alongside every app's own (§3).
    cases_client = None
    if cases is None:
        cases = cases_client = ReviewConsoleCases(os.environ.get("REVIEW_CONSOLE_URL", DEFAULT_REVIEW_CONSOLE_URL))
    registry.add(similar_past_cases.build_tool(cases))
    if faults := os.environ.get("TOOL_FAULTS"):
        registry = apply_faults(faults, registry)
    runner = ToolRunner(ResiliencePolicy.from_env(os.environ))
    registry_client = None
    if apps is None:
        apps = registry_client = RegistryClient(
            os.environ.get("REGISTRY_URL", DEFAULT_REGISTRY_URL),
            ttl_s=float(os.environ.get("MANIFEST_TTL_S", 30)),
        )

    # Stateless + JSON responses: every tool call is one request/response, so
    # any replica can serve any call (no sticky sessions across rollouts).
    session_manager = StreamableHTTPSessionManager(
        app=build_mcp_server(registry, apps, runner),
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with session_manager.run():
            yield
        if registry_client:
            await registry_client.aclose()
        if cases_client:
            await cases_client.aclose()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.state.registry = registry
    # A Route, not a Mount: a Mount would 307-redirect /mcp to /mcp/.
    app.router.routes.append(
        Route(MCP_PATH, StreamableHTTPASGIApp(session_manager), methods=["GET", "POST", "DELETE"])
    )

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
