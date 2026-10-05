"""Fakes for unit tests: a scripted LLM, an in-memory tool-gateway, and registry.

No test calls a real LLM or opens a network connection (docs/plan.md Day 3).
"""

import copy
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.struct_pb2 import Struct

from app.agent.llm import LLMResponse, Message, ToolCallRequest, ToolDefinition, Usage
from app.agent.loop import AgentRunner
from app.core.manifest import ManifestStore
from app.tools.gateway import ToolResult
from registry_client import AppNotFoundError
from proto_gen import agent_pb2
from run_context import RunContext

APP_ID = "test-app"

RUNBOOK_TOOL = {
    "tool_id": "lookup_runbook",
    "version": "1.0.0",
    "scope": "app",
    "description": "Look up a runbook.",
    "input_schema": {"type": "object"},
}

# `GET /apps/{APP_ID}` as registry serves it: each agent's `tools` already
# resolved (allowlisted, declared, enabled).
RESOLVED_APP: dict[str, Any] = {
    "app_id": APP_ID,
    "display_name": "Test app",
    "agents": [
        {
            "agent_id": "triage-agent",
            "version": "0.1.0",
            "role": "entry",
            "prompt_ref": "prompts/triage-agent.md",
            "tool_allowlist": ["lookup_runbook"],
            "invoke_on": [],
            "tools": [RUNBOOK_TOOL],
        },
        {
            "agent_id": "summarizer",
            "version": "0.1.0",
            "role": "callable",
            "prompt_ref": "prompts/summarizer.md",
            "tool_allowlist": [],
            "invoke_on": ["ESCALATE"],
            "tools": [],
        },
    ],
    "tools": [{"tool_id": "lookup_runbook", "version": "1.0.0", "scope": "app", "enabled": True}],
    "event_schema_ref": "event_schema.json",
    "alert_key_fields": ["alert_type", "host"],
    "memory_namespace": APP_ID,
    "escalate_when": [],
}

APP_PROMPT = "Call lookup_runbook with payload.alert_type, then decide."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def apps_dir(tmp_path: Path) -> Path:
    app_dir = tmp_path / APP_ID
    (app_dir / "prompts").mkdir(parents=True)
    (app_dir / "prompts" / "triage-agent.md").write_text(APP_PROMPT)
    (app_dir / "prompts" / "summarizer.md").write_text("Summarize.")
    return tmp_path


def make_request(**payload: Any) -> agent_pb2.RunAgentRequest:
    struct = Struct()
    struct.update(payload or {"alert_type": "disk_full", "host": "web-01"})
    return agent_pb2.RunAgentRequest(
        app_id=APP_ID,
        alert_id="alert-1",
        alert_key="disk_full:web-01",
        source="prometheus",
        severity="warning",
        message="Disk usage at 91% on web-01",
        timestamp_unix_ms=1_700_000_000_000,
        payload=struct,
    )


def tool_call(name: str = "lookup_runbook", call_id: str = "call-1", **arguments: Any) -> ToolCallRequest:
    args = arguments or {"alert_type": "disk_full"}
    return ToolCallRequest(id=call_id, name=name, arguments=args, raw_arguments=json.dumps(args))


def final(decision: str = "AUTO_RESOLVE", *reasons: str) -> LLMResponse:
    return LLMResponse(text=json.dumps({"decision": decision, "reasons": list(reasons) or ["RB-001 says so"]}))


@dataclass
class FakeLLM:
    """Returns the scripted responses in order and records what it was sent."""

    responses: list[LLMResponse]
    model: str = "fake-model"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": list(tools)})
        response = self.responses.pop(0)
        return LLMResponse(response.text, response.tool_calls, Usage(10, 5))


RUNBOOK_RESULT = {"found": True, "alert_type": "disk_full", "runbook": {"runbook_id": "RB-001"}}


class FakeRegistry:
    """`RegistryClient.get_app` over a dict of resolved apps."""

    def __init__(self, apps: dict[str, dict[str, Any]] | None = None):
        self.apps = apps if apps is not None else {APP_ID: copy.deepcopy(RESOLVED_APP)}

    async def get_app(self, app_id: str) -> dict[str, Any]:
        if app_id not in self.apps:
            raise AppNotFoundError(f"No app '{app_id}' is registered.")
        return self.apps[app_id]


@dataclass
class FakeGateway:
    results: dict[str, ToolResult] = field(
        default_factory=lambda: {"lookup_runbook": ToolResult(False, RUNBOOK_RESULT)}
    )
    calls: list[tuple[str, dict[str, Any], RunContext]] = field(default_factory=list)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["FakeGateway"]:
        yield self

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: RunContext) -> ToolResult:
        self.calls.append((name, arguments, context))
        return self.results[name]


def make_runner(
    apps_dir: Path,
    llm: FakeLLM,
    gateway: FakeGateway | None = None,
    max_tool_rounds: int = 5,
    registry: FakeRegistry | None = None,
):
    manifests = ManifestStore(registry or FakeRegistry(), apps_dir)
    return AgentRunner(manifests, gateway or FakeGateway(), llm, max_tool_rounds)
