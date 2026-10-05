"""MCP server over the tool registry: `tools/list` and `tools/call`.

Every failure comes back as a tool result with `is_error=True` and a
machine-readable `structured_content.error` code, never as a protocol error
or an empty result. `orchestrator` treats all of them like any other tool
failure (docs/ARCHITECTURE.md §10, §12):

- `tool_not_allowed`: the call has no run context, or the calling agent
  may not call this tool right now.
- `registry_unavailable`: the allowlist couldn't be checked (registry down,
  nothing cached). The call is refused: this fails closed.
- `tool_not_found`: no tool with that name was loaded.
- `invalid_arguments`: arguments failed the tool's input schema.
- `tool_failed`: the tool raised, or returned the wrong shape.

`orchestrator` sends the run's `app_id`/`agent_id` in each request's
`_meta` (`run_context`), never as a tool argument (§13 T4). Every call is
checked against that agent's tools as `registry` resolves them (allowlisted,
declared, enabled; same 30s cache as `orchestrator`), even though
`orchestrator` only offers those tools: defense in depth, so a tool missing
from the agent's definitions still can't be called (docs/plan.md Day 5).
`tools/list` is not filtered: `register_app.py` registers tools from it.
"""

import json
import logging
from typing import Any, Protocol

import mcp_types as types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from pydantic import ValidationError

from app.core.registry import ToolNotFoundError, ToolRegistry, ToolSpec
from registry_client import AppNotFoundError, RegistryUnavailableError
from run_context import RunContext, from_meta

logger = logging.getLogger(__name__)

SERVER_NAME = "tool-gateway"


class AppSource(Protocol):
    """`registry_client.RegistryClient`, or a fake in tests."""

    async def get_app(self, app_id: str) -> dict[str, Any]: ...


def build_mcp_server(registry: ToolRegistry, apps: AppSource) -> Server:
    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_to_mcp_tool(t) for t in registry.list()])

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        context = from_meta(params.meta)
        return await call_tool(registry, apps, params.name, params.arguments or {}, context)

    return Server(SERVER_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def call_tool(
    registry: ToolRegistry,
    apps: AppSource,
    name: str,
    arguments: dict[str, Any],
    context: RunContext | None,
) -> types.CallToolResult:
    if context is None:
        logger.warning("tools/call %s refused: no run context", name)
        return _error("tool_not_allowed", "Calls must carry a run context (app_id, agent_id).", tool_id=name)
    logger.info(
        "tools/call %s app_id=%s agent_id=%s alert_id=%s", name, context.app_id, context.agent_id, context.alert_id
    )
    try:
        allowed = await _allowed_tools(apps, context)
    except RegistryUnavailableError:
        logger.exception("tools/call %s refused: allowlist unavailable", name)
        return _error("registry_unavailable", "The caller's tool allowlist could not be checked.", tool_id=name)
    if name not in allowed:
        logger.warning("tools/call %s refused for app_id=%s agent_id=%s", name, context.app_id, context.agent_id)
        return _error(
            "tool_not_allowed",
            f"Agent '{context.agent_id}' of app '{context.app_id}' may not call '{name}'.",
            tool_id=name,
        )

    try:
        tool = registry.get(name)
    except ToolNotFoundError as e:
        return _error("tool_not_found", str(e), tool_id=name)
    if tool.scope == "app" and tool.app_id != context.app_id:
        # registry already refuses a manifest declaring another app's tool;
        # this holds even if tool ids were reused across apps.
        return _error("tool_not_allowed", f"'{name}' belongs to another app.", tool_id=name)

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


async def _allowed_tools(apps: AppSource, context: RunContext) -> set[str]:
    try:
        app = await apps.get_app(context.app_id)
    except AppNotFoundError:
        return set()
    for agent in app.get("agents", []):
        if agent.get("agent_id") == context.agent_id:
            return {t["tool_id"] for t in agent.get("tools", [])}
    return set()


def _to_mcp_tool(tool: ToolSpec) -> types.Tool:
    return types.Tool(
        name=tool.tool_id,
        description=tool.description,
        input_schema=tool.input_schema(),
        output_schema=tool.output_schema(),
        # Every tool is a read-only lookup (ADR-0004); registry enforces this
        # at registration, the hint lets any MCP client see it too.
        annotations=types.ToolAnnotations(read_only_hint=tool.read_only, destructive_hint=False),
        # register_app.py registers tools in registry from this listing.
        meta={"version": tool.version, "scope": tool.scope, "app_id": tool.app_id, "read_only": tool.read_only},
    )


def _error(code: str, message: str, **extra: Any) -> types.CallToolResult:
    structured = {"error": code, "message": message, **extra}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(structured))],
        structured_content=structured,
        is_error=True,
    )
