"""`MCPToolGateway` against an in-process MCP server standing in for tool-gateway."""

import json

import mcp_types as types
import pytest
from mcp.server import Server

from app.tools.gateway import GATEWAY_UNREACHABLE, MCPToolGateway
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


async def test_transport_failure_becomes_gateway_unreachable():
    class BrokenClient:
        async def call_tool(self, *args, **kwargs):
            raise ConnectionError("connection refused")

    from app.tools.gateway import MCPToolSession

    result = await MCPToolSession(BrokenClient()).call_tool("lookup_runbook", {}, context=CONTEXT)
    assert result.is_error
    assert result.error_code == GATEWAY_UNREACHABLE
