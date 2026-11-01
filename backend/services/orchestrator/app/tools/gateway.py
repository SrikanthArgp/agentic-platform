"""MCP client for `tool-gateway`.

`tool-gateway` is stateless (one request/response per call), so each MCP
request here opens its own short-lived client: a new HTTP connection
through the Service, to whichever replica is ready. Every `tools/call`
carries the run context (`app_id`, `agent_id`, `alert_id`) in the request
`_meta`, never in the tool arguments (docs/ARCHITECTURE.md §13 T4).

Why per call (Day 19): the MCP SDK runs a client's transport in a task
group, and a dropped connection fails that group, which cancels the task
that opened the client. With one client per agent run, a `tool-gateway`
pod going away mid-run cancelled the whole run ("agent run failed"). A
client per call confines that to the one call.

A dropped connection (the TCP connection reset or closed, or refused while
a pod leaves the Service) is retried exactly once on a new connection,
which reaches another pod. That's safe because every tool is read-only
(ADR-0004), and it's separate from `tool-gateway`'s own retries of a tool's
execution (ADR-0025). Nothing else is retried here: not a tool's error
result, not a timeout. A second dropped connection is an ordinary tool
failure (§10).

Failures come back as `ToolResult(is_error=True)` with an `error` code:
`tool-gateway`'s own codes (`tool_not_found`, `invalid_arguments`,
`tool_failed`, `tool_timeout`, `tool_unavailable`, `tool_version_mismatch`,
...), or `gateway_unreachable` when the call itself failed, including no
answer within `call_timeout_s`. `tool-gateway` bounds each call by its own
`TOOL_TIMEOUT_S` (ADR-0025); this timeout, set longer, only fires when
`tool-gateway` itself hangs, so no run waits on a tool forever.
`connect()` first checks that `tool-gateway` answers at all (an MCP
handshake, with the same one retry); if it doesn't, it raises
`GatewayUnavailableError` and the run is "not evaluated" (ESCALATE).
"""

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx2
from mcp import Client

from run_context import RunContext

logger = logging.getLogger(__name__)

GATEWAY_UNREACHABLE = "gateway_unreachable"

T = TypeVar("T")


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


DEFAULT_CALL_TIMEOUT_S = 10.0


class MCPToolGateway:
    def __init__(
        self,
        server: Any,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
        *,
        target: Callable[[], Any] | None = None,
    ):
        """`server` is tool-gateway's MCP URL, or an in-process MCP `Server`
        in tests. `target` (tests) builds what each new `Client` connects
        to, e.g. a transport over an HTTP client that drops connections."""
        self._target = target or (lambda: server)
        self._call_timeout_s = call_timeout_s

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["MCPToolSession"]:
        session = MCPToolSession(self._target, self._call_timeout_s)
        try:
            await session.with_client("handshake", lambda client: _noop())
        except Exception as e:
            raise GatewayUnavailableError(f"tool-gateway unreachable ({_root_cause(e)})") from e
        # Nothing stays open between calls, so the caller's own errors
        # inside this block propagate unchanged.
        yield session


async def _noop() -> None:
    return None


class MCPToolSession:
    def __init__(self, target: Callable[[], Any], call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S):
        self._target = target
        self._call_timeout_s = call_timeout_s

    async def with_client(self, what: str, use: Callable[[Client], Awaitable[T]]) -> T:
        """Run `use` on a new client; on a dropped connection, once more on
        another new one. Any other failure, or a second drop, raises."""
        for attempt in (1, 2):
            try:
                async with Client(self._target()) as client:
                    return await use(client)
            except Exception as e:
                if attempt == 1 and connection_dropped(e):
                    logger.warning("tool-gateway connection dropped during %s (%s); retrying once on a new connection",
                                   what, _root_cause(e))
                    continue
                raise
        raise AssertionError("unreachable")

    async def list_tools(self) -> list[GatewayTool]:
        result = await self.with_client("tools/list", lambda client: client.list_tools())
        tools = []
        for tool in result.tools:
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
        async def call(client: Client):
            return await client.call_tool(
                name, arguments, read_timeout_seconds=self._call_timeout_s, meta=context.to_meta()
            )

        try:
            result = await self.with_client(f"tools/call {name}", call)
        except Exception as e:
            logger.warning("tool-gateway call %s failed: %s", name, _root_cause(e))
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


def connection_dropped(e: BaseException) -> bool:
    """True when the failure is the connection itself (reset, closed, or
    refused), not a timeout and not anything the server answered. The MCP
    client reports transport errors inside exception groups."""
    leaves = list(_leaves(e))
    return any(isinstance(x, httpx2.TransportError) and not isinstance(x, httpx2.TimeoutException) for x in leaves)


def _leaves(e: BaseException):
    if isinstance(e, BaseExceptionGroup):
        for inner in e.exceptions:
            yield from _leaves(inner)
    else:
        yield e
        if e.__cause__ is not None:
            yield from _leaves(e.__cause__)


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
