"""`MCPToolGateway` against an in-process MCP server standing in for tool-gateway."""

import json
import time
from contextlib import asynccontextmanager

import anyio
import httpx2
import mcp_types as types
import pytest
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

from app.tools.gateway import GATEWAY_UNREACHABLE, GatewayUnavailableError, MCPToolGateway, connection_dropped
from run_context import RunContext, from_meta

pytestmark = pytest.mark.anyio

CONTEXT = RunContext(app_id="it-ops-triage", agent_id="triage-agent", alert_id="a-1")


def fake_tool_gateway(received: list) -> Server:
    async def on_list_tools(ctx, params):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="lookup_runbook",
                    description="Look up a runbook.",
                    input_schema={"type": "object", "properties": {"alert_type": {"type": "string"}}},
                    meta={"version": "1.0.0", "scope": "app", "app_id": "it-ops-triage"},
                )
            ]
        )

    async def on_call_tool(ctx, params):
        received.append((params.name, params.arguments, from_meta(params.meta)))
        if params.name == "hang":
            await anyio.sleep(10)
        if params.name == "boom":
            structured = {"error": "tool_failed", "message": "Tool 'boom' failed."}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(structured))],
                structured_content=structured,
                is_error=True,
            )
        structured = {"found": True, "alert_type": params.arguments["alert_type"]}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(structured))], structured_content=structured
        )

    return Server("fake-tool-gateway", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def test_list_tools_maps_schema_and_meta():
    async with MCPToolGateway(fake_tool_gateway([])).connect() as session:
        [tool] = await session.list_tools()

    assert tool.name == "lookup_runbook"
    assert tool.input_schema["properties"] == {"alert_type": {"type": "string"}}
    assert (tool.version, tool.scope, tool.app_id) == ("1.0.0", "app", "it-ops-triage")


async def test_call_sends_run_context_in_meta_and_only_tool_arguments():
    received: list = []
    async with MCPToolGateway(fake_tool_gateway(received)).connect() as session:
        result = await session.call_tool("lookup_runbook", {"alert_type": "disk_full"}, context=CONTEXT)

    assert not result.is_error
    assert result.content == {"found": True, "alert_type": "disk_full"}
    [(name, arguments, context)] = received
    assert name == "lookup_runbook"
    assert arguments == {"alert_type": "disk_full"}
    assert context == CONTEXT


async def test_error_result_keeps_the_gateway_error_code():
    async with MCPToolGateway(fake_tool_gateway([])).connect() as session:
        result = await session.call_tool("boom", {}, context=CONTEXT)

    assert result.is_error
    assert result.error_code == "tool_failed"


async def test_a_hung_gateway_call_times_out_as_gateway_unreachable():
    started = time.monotonic()
    async with MCPToolGateway(fake_tool_gateway([]), call_timeout_s=0.1).connect() as session:
        result = await session.call_tool("hang", {}, context=CONTEXT)

    assert result.error_code == GATEWAY_UNREACHABLE
    assert time.monotonic() - started < 5


async def test_unreachable_gateway_fails_to_connect_with_a_typed_error(unused_port):
    # Found on Day 11: with tool-gateway down, MCP initialize fails inside
    # connect(), before any call_tool, as an anyio exception group.
    with pytest.raises(GatewayUnavailableError, match=r"tool-gateway unreachable \(ConnectError"):
        async with MCPToolGateway(f"http://127.0.0.1:{unused_port}/mcp").connect():
            pass


async def test_errors_inside_the_session_are_not_mistaken_for_an_outage():
    # The MCP client's task group may wrap it in an exception group; either
    # way it must not be reported as tool-gateway being down.
    with pytest.raises((ValueError, BaseExceptionGroup)) as e:
        async with MCPToolGateway(fake_tool_gateway([])).connect():
            raise ValueError("caller bug")

    assert not isinstance(e.value, GatewayUnavailableError)
    assert isinstance(e.value, ValueError) or e.group_contains(ValueError, match="caller bug")


# --- Dropped connections (Day 19): over real Streamable HTTP, through an
# HTTP transport that drops chosen tools/call requests before they're sent.


class DroppingTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport, drop_calls: int):
        self.inner = inner
        self.drop_calls = drop_calls
        self.dropped = 0

    async def handle_async_request(self, request):
        if b'"tools/call"' in (request.content or b"") and self.dropped < self.drop_calls:
            self.dropped += 1
            raise httpx2.RemoteProtocolError("Server disconnected without sending a response.")
        return await self.inner.handle_async_request(request)


@asynccontextmanager
async def over_http(received: list, drop_calls: int):
    manager = StreamableHTTPSessionManager(app=fake_tool_gateway(received), json_response=True, stateless=True)
    app = Starlette(routes=[Route("/mcp", StreamableHTTPASGIApp(manager), methods=["GET", "POST", "DELETE"])])
    transport = DroppingTransport(httpx2.ASGITransport(app=app), drop_calls)
    async with manager.run(), httpx2.AsyncClient(transport=transport) as http:
        yield MCPToolGateway(
            None, target=lambda: streamable_http_client("http://localhost/mcp", http_client=http)
        ), transport


async def test_a_dropped_connection_is_retried_once_on_a_new_one():
    received: list = []
    async with over_http(received, drop_calls=1) as (gateway, transport):
        async with gateway.connect() as session:
            result = await session.call_tool("lookup_runbook", {"alert_type": "disk_full"}, context=CONTEXT)

    assert not result.is_error
    assert result.content == {"found": True, "alert_type": "disk_full"}
    assert transport.dropped == 1
    assert len(received) == 1  # the dropped attempt never reached the server


async def test_a_second_dropped_connection_is_an_ordinary_tool_failure():
    received: list = []
    async with over_http(received, drop_calls=5) as (gateway, transport):
        async with gateway.connect() as session:
            result = await session.call_tool("lookup_runbook", {"alert_type": "disk_full"}, context=CONTEXT)
            # The run's own task wasn't cancelled: the session still works.
            transport.drop_calls = 0
            after = await session.call_tool("lookup_runbook", {"alert_type": "x"}, context=CONTEXT)

    assert result.error_code == GATEWAY_UNREACHABLE
    assert transport.dropped == 2  # one try, exactly one retry
    assert not after.is_error


async def test_a_tool_error_is_never_retried():
    received: list = []
    async with over_http(received, drop_calls=0) as (gateway, _):
        async with gateway.connect() as session:
            result = await session.call_tool("boom", {}, context=CONTEXT)

    assert result.error_code == "tool_failed"
    assert [name for name, _, _ in received] == ["boom"]


async def test_a_timeout_is_never_retried():
    received: list = []
    async with MCPToolGateway(fake_tool_gateway(received), call_timeout_s=0.1).connect() as session:
        result = await session.call_tool("hang", {}, context=CONTEXT)

    assert result.error_code == GATEWAY_UNREACHABLE
    assert [name for name, _, _ in received] == ["hang"]


def test_only_transport_failures_count_as_a_dropped_connection():
    dropped = httpx2.RemoteProtocolError("Server disconnected")
    assert connection_dropped(ExceptionGroup("tg", [ExceptionGroup("inner", [dropped])]))
    assert connection_dropped(httpx2.ConnectError("connection refused"))
    assert not connection_dropped(httpx2.ReadTimeout("timed out"))
    assert not connection_dropped(ValueError("bad result"))
