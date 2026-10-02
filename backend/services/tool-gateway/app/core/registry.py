"""The tool table `tool-gateway` serves from: `tool_id` -> `ToolSpec`.

Built once at startup (docs/ARCHITECTURE.md §12; hot reload was rejected in
ADR-0013). Being loaded here does not mean a caller may use a tool: which
agent may call which tool is decided per request by its `tool_allowlist`
(§3), not by what was loaded.
"""

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel


class ToolNotFoundError(LookupError):
    def __init__(self, tool_id: str):
        super().__init__(f"No tool with tool_id '{tool_id}' is loaded in tool-gateway.")
        self.tool_id = tool_id


class DuplicateToolError(ValueError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    version: str
    description: str
    scope: Literal["app", "global"]
    # The owning app for scope="app"; None for global tools.
    app_id: str | None
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[[Any], BaseModel | Awaitable[BaseModel]]

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()

    async def invoke(self, arguments: dict[str, Any]) -> BaseModel:
        """Validate `arguments` against the input model and run the handler.

        Raises pydantic.ValidationError on invalid arguments, and TypeError if
        the handler returns something other than the declared output model.
        """
        args = self.input_model.model_validate(arguments)
        result = self.handler(args)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, self.output_model):
            raise TypeError(
                f"Tool '{self.tool_id}' returned {type(result).__name__}, "
                f"expected {self.output_model.__name__}."
            )
        return result


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolSpec] = ()):
        self._tools: dict[str, ToolSpec] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: ToolSpec) -> None:
        existing = self._tools.get(tool.tool_id)
        if existing is not None:
            raise DuplicateToolError(
                f"tool_id '{tool.tool_id}' is registered twice "
                f"(app_id={existing.app_id!r} and app_id={tool.app_id!r})."
            )
        self._tools[tool.tool_id] = tool

    def get(self, tool_id: str) -> ToolSpec:
        try:
            return self._tools[tool_id]
        except KeyError:
            raise ToolNotFoundError(tool_id) from None

    def list(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.tool_id)

    def __len__(self) -> int:
        return len(self._tools)
