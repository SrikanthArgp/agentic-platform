"""MCP client for `tool-gateway`.

One MCP connection per agent run (`connect()`); `tool-gateway` is stateless,
so any replica serves any call. Every `tools/call` carries the run context
(`app_id`, `agent_id`, `alert_id`) in the request `_meta`, never in the
tool arguments (docs/ARCHITECTURE.md §13 T4).

Failures come back as `ToolResult(is_error=True)` with an `error` code:
`tool-gateway`'s own codes (`tool_not_found`, `invalid_arguments`,
`tool_failed`), or `gateway_unreachable` when the call itself failed.
If the connection can't even be opened, `connect()` raises
`GatewayUnavailableError` and the run is "not evaluated" (ESCALATE).
"""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from mcp import Client

from run_context import RunContext

logger = logging.getLogger(__name__)

GATEWAY_UNREACHABLE = "gateway_unreachable"


class GatewayUnavailableError(RuntimeError):
    """The MCP connection to `tool-gateway` couldn't be opened."""


@dataclass(frozen=True)
class GatewayTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    version: str = ""
    scope: str = ""
    # Owning app for scope="app" tools; None for global tools.
    app_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    is_error: bool
    content: dict[str, Any]

    @property
    def error_code(self) -> str | None:
        return str(self.content.get("error", "unknown")) if self.is_error else None


class ToolSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: RunContext) -> ToolResult: ...


class ToolGateway(Protocol):
    def connect(self) -> AbstractAsyncContextManager[ToolSession]: ...


class MCPToolGateway:
    def __init__(self, server: Any):
        """`server` is tool-gateway's MCP URL, or an in-process MCP `Server` in tests."""
        self._server = server

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["MCPToolSession"]:
        async with AsyncExitStack() as stack:
            # Only opening the session (MCP initialize) is mapped; errors
            # raised by the caller inside the block propagate unchanged.
            try:
                client = await stack.enter_async_context(Client(self._server))
            except Exception as e:
                raise GatewayUnavailableError(f"tool-gateway unreachable ({_root_cause(e)})") from e
            yield MCPToolSession(client)


class MCPToolSession:
    def __init__(self, client: Client):
        self._client = client

    async def list_tools(self) -> list[GatewayTool]:
        tools = []
        for tool in (await self._client.list_tools()).tools:
            meta = tool.meta or {}
            tools.append(
                GatewayTool(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                    version=str(meta.get("version", "")),
                    scope=str(meta.get("scope", "")),
                    app_id=meta.get("app_id"),
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: RunContext) -> ToolResult:
        try:
            result = await self._client.call_tool(name, arguments, meta=context.to_meta())
        except Exception:
            logger.exception("tool-gateway call %s failed", name)
            return ToolResult(is_error=True, content={"error": GATEWAY_UNREACHABLE, "tool_id": name})
        content = result.structured_content
        if content is None:
            # Not expected from tool-gateway (it always sets structured
            # content), but any MCP server may answer with text only.
            text = "".join(getattr(c, "text", "") for c in result.content)
            content = _json_object(text) or {"text": text}
        if result.is_error and "error" not in content:
            content = {"error": "tool_failed", **content}
        return ToolResult(is_error=bool(result.is_error), content=content)


def _root_cause(e: BaseException) -> str:
    """The first leaf of an exception group (the MCP client wraps transport
    errors in anyio task groups), as `Type: message`."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
