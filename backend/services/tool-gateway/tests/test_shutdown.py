"""SIGTERM against a real uvicorn server (docs/plan.md Day 19): readiness
flips at once, and an MCP call already in flight still gets its result."""

import asyncio
import signal
import socket

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.main import MCP_PATH, create_app
from run_context import RunContext
from tests.conftest import FakeApps, FakeCases

pytestmark = pytest.mark.anyio


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def harmless_sigterm():
    # uvicorn re-raises the signal it caught once it has exited; make that a no-op.
    original = signal.signal(signal.SIGTERM, lambda *_: None)
    yield
    signal.signal(signal.SIGTERM, original)


async def test_sigterm_flips_readiness_and_finishes_the_in_flight_tool_call(monkeypatch, harmless_sigterm):
    monkeypatch.setenv("TOOL_FAULTS", "lookup_runbook=sleep:0.5")
    apps = FakeApps({"it-ops-triage": {"triage-agent": ["lookup_runbook"]}}, versions={"lookup_runbook": "1.0.0"})
    app = create_app(apps=apps, cases=FakeCases([]))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    url = f"http://127.0.0.1:{port}"

    async with httpx.AsyncClient(base_url=url) as probe:
        assert (await probe.get("/readyz")).status_code == 200

    async def call():
        async with httpx2.AsyncClient() as http:
            async with Client(streamable_http_client(f"{url}{MCP_PATH}", http_client=http)) as mcp:
                # The client validates a result against the tool's output
                # schema, listing tools if it hasn't yet; list first so that
                # follow-up isn't sent to a server that has already exited.
                await mcp.list_tools()
                meta = RunContext(app_id="it-ops-triage", agent_id="triage-agent").to_meta()
                return await mcp.call_tool("lookup_runbook", {"alert_type": "disk_full"}, meta=meta)

    in_flight = asyncio.create_task(call())
    await asyncio.sleep(0.3)  # the tool is sleeping
    signal.raise_signal(signal.SIGTERM)
    assert app.state.lifecycle.draining is True

    result = await in_flight
    await asyncio.wait_for(serving, 5)

    assert not result.is_error
    assert result.structured_content["runbook"]["runbook_id"] == "RB-001"
