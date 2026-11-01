import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main
from app.core.config import Settings
from app.main import HealthResponse, app, base_url, readiness_checks

client = TestClient(app)

pytestmark = pytest.mark.anyio


def test_healthz_returns_200_with_expected_shape():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "orchestrator"}


def test_readyz_is_503_until_startup_has_run():
    # Without the lifespan nothing has started: no consumer, no clients.
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["reasons"] == ["starting"]


def test_health_response_rejects_missing_fields():
    with pytest.raises(ValidationError):
        HealthResponse(status="ok")


def test_base_url_drops_the_mcp_path():
    assert base_url("http://tool-gateway:8000/mcp") == "http://tool-gateway:8000"


class Pipeline:
    def __init__(self, ready: bool):
        self.ready = ready


def http(statuses: dict[str, int | Exception]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        outcome = statuses[request.url.host]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


SETTINGS = Settings(registry_url="http://registry:8000", tool_gateway_url="http://tool-gateway:8000/mcp")


async def reasons(pipeline, statuses, monkeypatch, draining=False) -> list[str]:
    life = main.Lifecycle("orchestrator")
    life.mark_started()
    if draining:
        life.begin_drain()
    monkeypatch.setattr(main, "life", life)
    async with http(statuses) as client_:
        return await life.not_ready_reasons(readiness_checks(SETTINGS, pipeline, client_))


async def test_ready_when_joined_and_both_dependencies_answer(monkeypatch):
    assert await reasons(Pipeline(True), {"registry": 200, "tool-gateway": 200}, monkeypatch) == []


async def test_not_ready_until_the_consumer_has_joined(monkeypatch):
    got = await reasons(Pipeline(False), {"registry": 200, "tool-gateway": 200}, monkeypatch)
    assert got == ["kafka: consumer hasn't joined its group"]


async def test_not_ready_when_registry_or_tool_gateway_is_unreachable(monkeypatch):
    got = await reasons(
        Pipeline(True), {"registry": httpx.ConnectError("refused"), "tool-gateway": 503}, monkeypatch
    )
    assert got == ["registry: unreachable (ConnectError)", "tool-gateway: HTTP 503"]


async def test_kafka_disabled_skips_the_consumer_check(monkeypatch):
    assert await reasons(None, {"registry": 200, "tool-gateway": 200}, monkeypatch) == []


async def test_draining_is_not_ready_whatever_the_dependencies(monkeypatch):
    got = await reasons(Pipeline(True), {"registry": 200, "tool-gateway": 200}, monkeypatch, draining=True)
    assert got == ["draining"]
