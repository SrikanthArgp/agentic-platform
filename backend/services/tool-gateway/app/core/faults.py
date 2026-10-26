"""Deliberately broken tools, for resilience tests on a live stack (docs/plan.md Day 12).

`TOOL_FAULTS` is a comma-separated list of `tool_id=fault`:

- `error`: the handler raises on every call;
- `sleep:SECONDS`: the handler blocks that long, then runs normally.

e.g. `TOOL_FAULTS=lookup_runbook=sleep:30`. Unset in every committed
config: it exists only to break a tool on purpose, and startup logs a
WARNING for each fault so it can't go unnoticed.
"""

import dataclasses
import logging
import time
from collections.abc import Callable
from typing import Any

from app.core.registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)


class InjectedFaultError(RuntimeError):
    pass


def apply_faults(spec: str, registry: ToolRegistry) -> ToolRegistry:
    """A copy of `registry` with the faults in `spec` applied. Raises ValueError on a bad spec."""
    faults = _parse(spec)
    tools = []
    for tool in registry.list():
        fault = faults.pop(tool.tool_id, None)
        if fault is not None:
            logger.warning("TOOL_FAULTS: tool %s is deliberately broken (%s)", tool.tool_id, fault)
            tool = dataclasses.replace(tool, handler=_broken(tool, fault))
        tools.append(tool)
    if faults:
        raise ValueError(f"TOOL_FAULTS names tools that aren't loaded: {sorted(faults)}")
    return ToolRegistry(tools)


def _parse(spec: str) -> dict[str, str]:
    faults = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        tool_id, sep, fault = item.partition("=")
        if not sep or not (fault == "error" or (fault.startswith("sleep:") and _seconds(fault) is not None)):
            raise ValueError(f"TOOL_FAULTS: bad entry {item!r} (want tool_id=error or tool_id=sleep:SECONDS)")
        faults[tool_id] = fault
    return faults


def _seconds(fault: str) -> float | None:
    try:
        s = float(fault.removeprefix("sleep:"))
    except ValueError:
        return None
    return s if s >= 0 else None


def _broken(tool: ToolSpec, fault: str) -> Callable[[Any], Any]:
    if fault == "error":
        def handler(args: Any) -> Any:
            raise InjectedFaultError(f"injected fault for tool '{tool.tool_id}'")
        return handler

    seconds = _seconds(fault)
    original = tool.handler

    # Sync, so it holds a worker thread the way a hung blocking call would.
    def handler(args: Any) -> Any:
        time.sleep(seconds)
        return original(args)
    return handler
