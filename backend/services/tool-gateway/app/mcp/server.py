"""MCP server over the tool registry: `tools/list` and `tools/call`.

Every failure comes back as a tool result with `is_error=True` and a
machine-readable `structured_content.error` code, never as a protocol error
or an empty result. `orchestrator` treats all of them like any other tool
failure (docs/ARCHITECTURE.md §10, §12):

- `tool_not_found`: no tool with that name was loaded.
- `invalid_arguments`: arguments failed the tool's input schema.
- `tool_failed`: the tool raised, or returned the wrong shape.

Not yet: per-agent `tool_allowlist` checks and the `app_id`/`agent_id` run
context from `orchestrator` (§13 T4; plan.md Days 3 and 13).
"""

import json
import logging
from typing import Any

import mcp_types as types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from pydantic import ValidationError

from app.core.registry import ToolNotFoundError, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

SERVER_NAME = "tool-gateway"


def build_mcp_server(registry: ToolRegistry) -> Server:
    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_to_mcp_tool(t) for t in registry.list()])

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await call_tool(registry, params.name, params.arguments or {})

    return Server(SERVER_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def call_tool(registry: ToolRegistry, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    try:
        tool = registry.get(name)
    except ToolNotFoundError as e:
        return _error("tool_not_found", str(e), tool_id=name)

    try:
        output = await tool.invoke(arguments)
    except ValidationError as e:
        errors = e.errors(include_url=False, include_context=False, include_input=False)
        return _error("invalid_arguments", f"Invalid arguments for '{name}'.", tool_id=name, details=errors)
    except Exception:
        logger.exception("tool %s raised", name)
        return _error("tool_failed", f"Tool '{name}' failed.", tool_id=name)

    structured = output.model_dump(mode="json")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(structured))],
        structured_content=structured,
    )


def _to_mcp_tool(tool: ToolSpec) -> types.Tool:
    return types.Tool(
        name=tool.tool_id,
        description=tool.description,
        input_schema=tool.input_schema(),
        output_schema=tool.output_schema(),
        # Every tool is a read-only lookup (ADR-0004); registry enforces this
        # at registration, the hint lets any MCP client see it too.
        annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False),
        meta={"version": tool.version, "scope": tool.scope, "app_id": tool.app_id},
    )


def _error(code: str, message: str, **extra: Any) -> types.CallToolResult:
    structured = {"error": code, "message": message, **extra}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(structured))],
        structured_content=structured,
        is_error=True,
    )
