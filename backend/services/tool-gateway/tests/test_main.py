from contextlib import asynccontextmanager

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from app.main import MCP_PATH, HealthResponse, app, create_app

client = TestClient(app)


def test_healthz_returns_200_with_expected_shape():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "tool-gateway"}


def test_health_response_rejects_missing_fields():
    with pytest.raises(ValidationError):
        HealthResponse(status="ok")


# --- MCP over HTTP, through the real app (startup scan of backend/apps) ---

HOST = "localhost:8003"


@asynccontextmanager
async def http_to_app():
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=f"http://{HOST}") as http:
            yield http


@pytest.mark.anyio
async def test_mcp_over_http_lists_and_calls_lookup_runbook():
    async with http_to_app() as http:
        async with Client(streamable_http_client(f"http://{HOST}{MCP_PATH}", http_client=http)) as mcp:
            names = [t.name for t in (await mcp.list_tools()).tools]
            found = await mcp.call_tool("lookup_runbook", {"alert_type": "disk_full"})
            missing = await mcp.call_tool("lookup_runbook", {"alert_type": "made_up_alert"})

    assert "lookup_runbook" in names
    assert found.structured_content["runbook"]["runbook_id"] == "RB-001"
    assert missing.structured_content["found"] is False
    assert not missing.is_error


@pytest.mark.anyio
async def test_mcp_rejects_unlisted_host_header():
    # DNS-rebinding protection: only configured Host headers reach the server.
    async with http_to_app() as http:
        response = await http.post(
            MCP_PATH,
            headers={"Host": "evil.example", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )

    assert response.status_code == 421
