"""The run context `orchestrator` attaches to every MCP request.

Which app and agent a tool call is made for travels in the MCP request's
`_meta`, set by `orchestrator`, never as a tool argument: tool schemas don't
expose `app_id`, so text injected into a prompt can't make the LLM call a
tool on another app's behalf (docs/ARCHITECTURE.md §3, §13 T4).

`tool-gateway` reads it back with `from_meta()`. Day 3 only logs it; Day 13
uses it to enforce the agent's `tool_allowlist` and to scope global tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_PREFIX = "agentic-platform/"
APP_ID_KEY = _PREFIX + "app_id"
AGENT_ID_KEY = _PREFIX + "agent_id"
ALERT_ID_KEY = _PREFIX + "alert_id"


@dataclass(frozen=True)
class RunContext:
    app_id: str
    agent_id: str
    alert_id: str = ""

    def to_meta(self) -> dict[str, str]:
        return {APP_ID_KEY: self.app_id, AGENT_ID_KEY: self.agent_id, ALERT_ID_KEY: self.alert_id}


def from_meta(meta: Mapping[str, Any] | None) -> RunContext | None:
    """The run context in an MCP request's `_meta`, or None if it has none."""
    if not meta:
        return None
    app_id = meta.get(APP_ID_KEY)
    agent_id = meta.get(AGENT_ID_KEY)
    if not isinstance(app_id, str) or not app_id or not isinstance(agent_id, str) or not agent_id:
        return None
    alert_id = meta.get(ALERT_ID_KEY)
    return RunContext(app_id=app_id, agent_id=agent_id, alert_id=alert_id if isinstance(alert_id, str) else "")
