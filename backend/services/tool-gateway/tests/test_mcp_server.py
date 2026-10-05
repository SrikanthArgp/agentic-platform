"""The MCP server, driven by a real MCP client connected in-process."""

import pytest
from mcp import Client

from app.core.loader import load_app_tools
from app.core.registry import ToolRegistry
from app.mcp.server import build_mcp_server
from run_context import RunContext
from tests.conftest import FakeApps, echo_module, write_tool_module

pytestmark = pytest.mark.anyio

ALL_ECHO_TOOLS = ["echo", "async_echo", "broken", "wrong_shape"]
CTX = RunContext(app_id="app-one", agent_id="triage-agent", alert_id="a-1").to_meta()


def build(tmp_path, apps: FakeApps):
    write_tool_module(tmp_path, "app-one", "echo_tools", echo_module())
    write_tool_module(tmp_path, "app-two", "other_tools", echo_module("two_"))
    registry = ToolRegistry()
    load_app_tools(tmp_path, registry)
    return build_mcp_server(registry, apps)


@pytest.fixture
def apps():
    return FakeApps({"app-one": {"triage-agent": [*ALL_ECHO_TOOLS, "no_such_tool", "two_echo"], "summarizer": []}})


@pytest.fixture
def server(tmp_path, apps):
    return build(tmp_path, apps)


async def test_list_tools_exposes_explicit_schemas_and_read_only_hint(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    echo = tools["echo"]
    assert echo.description == "Echo the input back."
    assert echo.input_schema["required"] == ["text"]
    assert echo.input_schema["additionalProperties"] is False
    assert echo.output_schema["properties"]["echoed"]["type"] == "string"
    assert echo.annotations.read_only_hint is True
    assert echo.meta == {"version": "0.1.0", "scope": "app", "app_id": "app-one", "read_only": True}


@pytest.mark.parametrize("tool, expected", [("echo", "hi"), ("async_echo", "HI")])
async def test_call_returns_structured_output(server, tool, expected):
    async with Client(server) as client:
        result = await client.call_tool(tool, {"text": "hi"}, meta=CTX)

    assert not result.is_error
    assert result.structured_content == {"echoed": expected}


async def test_unknown_tool_returns_tool_not_found_error(server):
    async with Client(server) as client:
        result = await client.call_tool("no_such_tool", {}, meta=CTX)

    assert result.is_error
    assert result.structured_content["error"] == "tool_not_found"
    assert result.structured_content["tool_id"] == "no_such_tool"


@pytest.mark.parametrize("arguments", [{}, {"text": 5}, {"text": "hi", "extra": 1}])
async def test_invalid_arguments_return_invalid_arguments_error(server, arguments):
    async with Client(server) as client:
        result = await client.call_tool("echo", arguments, meta=CTX)

    assert result.is_error
    assert result.structured_content["error"] == "invalid_arguments"
    assert result.structured_content["details"]


@pytest.mark.parametrize("tool", ["broken", "wrong_shape"])
async def test_tool_failure_returns_tool_failed_error_without_internals(server, tool):
    async with Client(server) as client:
        result = await client.call_tool(tool, {"text": "hi"}, meta=CTX)

    assert result.is_error
    assert result.structured_content["error"] == "tool_failed"
    assert "fixture backend unavailable" not in result.content[0].text


async def test_call_reads_run_context_from_meta_not_arguments(server, caplog):
    caplog.set_level("INFO", logger="app.mcp.server")
    async with Client(server) as client:
        result = await client.call_tool("echo", {"text": "hi"}, meta=CTX)

    assert not result.is_error
    assert "tools/call echo app_id=app-one agent_id=triage-agent alert_id=a-1" in caplog.text


async def test_call_without_run_context_is_refused(server):
    async with Client(server) as client:
        result = await client.call_tool("echo", {"text": "hi"})

    assert result.is_error
    assert result.structured_content["error"] == "tool_not_allowed"


@pytest.mark.parametrize(
    "app_id, agent_id",
    [("app-one", "summarizer"), ("app-one", "no-such-agent"), ("no-such-app", "triage-agent")],
    ids=["not-on-allowlist", "unknown-agent", "unknown-app"],
)
async def test_call_outside_the_agents_tools_is_refused_before_running(tmp_path, app_id, agent_id):
    server = build(tmp_path, FakeApps({"app-one": {"triage-agent": ["echo"], "summarizer": []}}))
    meta = RunContext(app_id=app_id, agent_id=agent_id).to_meta()
    async with Client(server) as client:
        result = await client.call_tool("broken", {"text": "hi"}, meta=meta)

    # tool_not_allowed, not tool_failed: the handler never ran.
    assert result.is_error
    assert result.structured_content["error"] == "tool_not_allowed"


async def test_disabled_tool_is_refused(tmp_path):
    """registry drops a disabled tool from the agent's tools: the allowlist alone isn't enough."""
    server = build(tmp_path, FakeApps({"app-one": {"triage-agent": []}}))
    async with Client(server) as client:
        result = await client.call_tool("echo", {"text": "hi"}, meta=CTX)

    assert result.structured_content["error"] == "tool_not_allowed"


async def test_another_apps_tool_is_refused_even_if_listed(server):
    async with Client(server) as client:
        result = await client.call_tool("two_echo", {"text": "hi"}, meta=CTX)

    assert result.structured_content["error"] == "tool_not_allowed"
    assert "another app" in result.structured_content["message"]


async def test_registry_down_fails_closed(tmp_path):
    server = build(tmp_path, FakeApps({}, down=True))
    async with Client(server) as client:
        result = await client.call_tool("echo", {"text": "hi"}, meta=CTX)

    assert result.is_error
    assert result.structured_content["error"] == "registry_unavailable"
