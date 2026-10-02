"""The MCP server, driven by a real MCP client connected in-process."""

import pytest
from mcp import Client

from app.core.loader import load_app_tools
from app.core.registry import ToolRegistry
from app.mcp.server import build_mcp_server
from tests.conftest import echo_module, write_tool_module

pytestmark = pytest.mark.anyio


@pytest.fixture
def server(tmp_path):
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module())
    registry = ToolRegistry()
    load_app_tools(tmp_path, registry)
    return build_mcp_server(registry)


async def test_list_tools_exposes_explicit_schemas_and_read_only_hint(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    echo = tools["echo"]
    assert echo.description == "Echo the input back."
    assert echo.input_schema["required"] == ["text"]
    assert echo.input_schema["additionalProperties"] is False
    assert echo.output_schema["properties"]["echoed"]["type"] == "string"
    assert echo.annotations.read_only_hint is True
    assert echo.meta == {"version": "0.1.0", "scope": "app", "app_id": "app-one"}


@pytest.mark.parametrize("tool, expected", [("echo", "hi"), ("async_echo", "HI")])
async def test_call_returns_structured_output(server, tool, expected):
    async with Client(server) as client:
        result = await client.call_tool(tool, {"text": "hi"})

    assert not result.is_error
    assert result.structured_content == {"echoed": expected}


async def test_unknown_tool_returns_tool_not_found_error(server):
    async with Client(server) as client:
        result = await client.call_tool("no_such_tool", {})

    assert result.is_error
    assert result.structured_content["error"] == "tool_not_found"
    assert result.structured_content["tool_id"] == "no_such_tool"


@pytest.mark.parametrize("arguments", [{}, {"text": 5}, {"text": "hi", "extra": 1}])
async def test_invalid_arguments_return_invalid_arguments_error(server, arguments):
    async with Client(server) as client:
        result = await client.call_tool("echo", arguments)

    assert result.is_error
    assert result.structured_content["error"] == "invalid_arguments"
    assert result.structured_content["details"]


@pytest.mark.parametrize("tool", ["broken", "wrong_shape"])
async def test_tool_failure_returns_tool_failed_error_without_internals(server, tool):
    async with Client(server) as client:
        result = await client.call_tool(tool, {"text": "hi"})

    assert result.is_error
    assert result.structured_content["error"] == "tool_failed"
    assert "fixture backend unavailable" not in result.content[0].text
