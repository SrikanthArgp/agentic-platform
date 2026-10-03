"""Fakes for unit tests: a scripted LLM and an in-memory tool-gateway.

No test calls a real LLM or opens a network connection (docs/plan.md Day 3).
"""

import json
import textwrap
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.struct_pb2 import Struct

from app.agent.llm import LLMResponse, Message, ToolCallRequest, ToolDefinition, Usage
from app.agent.loop import AgentRunner
from app.core.manifest import FileManifestStore
from app.tools.gateway import GatewayTool, ToolResult
from proto_gen import agent_pb2
from run_context import RunContext

APP_ID = "test-app"

MANIFEST = f"""
app_id: {APP_ID}
display_name: Test app
agents:
  - agent_id: triage-agent
    version: 0.1.0
    role: entry
    prompt_ref: prompts/triage-agent.md
    tool_allowlist: [lookup_runbook]
  - agent_id: summarizer
    version: 0.1.0
    role: callable
    prompt_ref: prompts/summarizer.md
    tool_allowlist: []
    invoke_on: [ESCALATE]
tools:
  - tool_id: lookup_runbook
    version: 1.0.0
    scope: app
"""

APP_PROMPT = "Call lookup_runbook with payload.alert_type, then decide."


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def apps_dir(tmp_path: Path) -> Path:
    app_dir = tmp_path / APP_ID
    (app_dir / "prompts").mkdir(parents=True)
    (app_dir / "manifest.yaml").write_text(textwrap.dedent(MANIFEST))
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


@dataclass
class FakeGateway:
    tools: list[GatewayTool] = field(
        default_factory=lambda: [
            GatewayTool("lookup_runbook", "Look up a runbook.", {"type": "object"}, "1.0.0", "app", APP_ID),
            GatewayTool("other_app_tool", "Not ours.", {"type": "object"}, "1.0.0", "app", "other-app"),
        ]
    )
    results: dict[str, ToolResult] = field(
        default_factory=lambda: {"lookup_runbook": ToolResult(False, RUNBOOK_RESULT)}
    )
    calls: list[tuple[str, dict[str, Any], RunContext]] = field(default_factory=list)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["FakeGateway"]:
        yield self

    async def list_tools(self) -> list[GatewayTool]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: RunContext) -> ToolResult:
        self.calls.append((name, arguments, context))
        return self.results[name]


def make_runner(apps_dir: Path, llm: FakeLLM, gateway: FakeGateway | None = None, max_tool_rounds: int = 5):
    return AgentRunner(FileManifestStore(apps_dir), gateway or FakeGateway(), llm, max_tool_rounds)
