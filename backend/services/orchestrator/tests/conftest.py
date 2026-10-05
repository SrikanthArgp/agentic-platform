"""Fakes for unit tests: a scripted chat model, an in-memory tool-gateway, registry, and memory-store.

No test calls a real LLM or opens a network connection (docs/plan.md Day 3).
"""

import asyncio
import copy
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.struct_pb2 import Struct

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool

from app.agent.loop import AgentRunner
from app.core.manifest import ManifestStore
from app.memory.client import ContextUnavailableError
from app.tools.gateway import ToolResult
from registry_client import AppNotFoundError
from proto_gen import agent_pb2, memory_store_pb2
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
            # Empty here so only Day 8's delegation tests (which set it) run it
            # after a decision; RunAgent can still run it directly by agent_id.
            "invoke_on": [],
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
def unused_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


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


USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


def tool_call(name: str = "lookup_runbook", call_id: str = "call-1", **arguments: Any) -> AIMessage:
    """A model reply asking for one tool call."""
    return tool_calls((name, call_id, arguments or {"alert_type": "disk_full"}))


def tool_calls(*calls: tuple[str, str, dict[str, Any]]) -> AIMessage:
    """A model reply asking for several tool calls: (name, id, args) each."""
    return AIMessage(
        content="", tool_calls=[{"name": n, "id": i, "args": a} for n, i, a in calls], usage_metadata=USAGE
    )


def text(content: str) -> AIMessage:
    """A final reply with arbitrary text."""
    return AIMessage(content=content, usage_metadata=USAGE)


def explanation(*reasons: str) -> AIMessage:
    """A callable agent's final reply."""
    return text(json.dumps({"reasons": list(reasons)}))


def final(decision: str = "AUTO_RESOLVE", *reasons: str, confidence: float = 0.9) -> AIMessage:
    return text(json.dumps(
        {"decision": decision, "confidence": confidence, "reasons": list(reasons) or ["RB-001 says so"]}
    ))


def context(
    *, novel: bool = False, incident_history: bool = False, **windows: dict[str, int]
) -> memory_store_pb2.GetContextResponse:
    """A `GetContextResponse` for `make_request()`'s key. `windows`: e.g.
    `window_24h={"alert_count": 3}`."""
    ctx = memory_store_pb2.GetContextResponse(
        app_id=APP_ID, alert_key="disk_full:web-01",
        is_novel_alert=novel, has_confirmed_incident_history=incident_history,
    )
    for name, counts in windows.items():
        getattr(ctx, name).CopyFrom(memory_store_pb2.ContextAggregate(**counts))
    return ctx


# A key seen before, nothing remarkable: no supervisor cap applies.
KNOWN_CONTEXT = context(window_7d={"alert_count": 3, "suppression_count": 1})


@dataclass
class FakeMemory:
    """`MemoryStoreClient.get_context`: `context`, or ContextUnavailableError when it's None."""

    context: memory_store_pb2.GetContextResponse | None = field(default_factory=lambda: KNOWN_CONTEXT)
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def get_context(self, app_id: str, alert_key: str) -> memory_store_pb2.GetContextResponse:
        self.calls.append((app_id, alert_key))
        if self.context is None:
            raise ContextUnavailableError("UNAVAILABLE: connection refused")
        return self.context


class FakeChatModel(BaseChatModel):
    """A LangChain chat model that returns scripted replies in order (an
    Exception in the script is raised instead) and records every call: the
    system prompt, the other messages, and the tools bound at the time.

    `scripts` gives some agents their own replies: the first key found in
    the system prompt (e.g. a callable's prompt text) picks that list, and
    `delays` (same keys) makes those calls slow. Everything else uses
    `responses`. Agents running in parallel never take each other's replies.
    """

    responses: list[Any] = []  # AIMessage, or an Exception to raise
    scripts: dict[str, list[Any]] = {}
    delays: dict[str, float] = {}
    calls: list[dict[str, Any]] = []
    bound: list[BaseTool] = []

    @property
    def _llm_type(self) -> str:
        return "fake-chat-model"

    def bind_tools(self, tools: list[BaseTool], **kwargs: Any) -> "FakeChatModel":
        # A copy per binding: parallel agents each keep their own tools.
        return self.model_copy(update={"bound": list(tools)})

    def _script(self, system: str) -> str | None:
        return next((key for key in self.scripts if key in system), None)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
        # model_copy shares these lists with the original, so one record of every call.
        self.calls.append({
            "system": system,
            "messages": [m for m in messages if not isinstance(m, SystemMessage)],
            "tools": list(self.bound),
        })
        key = self._script(system)
        response = (self.scripts[key] if key is not None else self.responses).pop(0)
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
        key = self._script(system)
        if key is not None and self.delays.get(key):
            await asyncio.sleep(self.delays[key])
        return self._generate(messages, stop, run_manager, **kwargs)


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
    llm: FakeChatModel,
    gateway: FakeGateway | None = None,
    max_tool_rounds: int = 5,
    registry: FakeRegistry | None = None,
    memory: FakeMemory | None = None,
    min_confidence: float = 0.6,
    callable_timeout_s: float = 30.0,
):
    manifests = ManifestStore(registry or FakeRegistry(), apps_dir)
    return AgentRunner(
        manifests, gateway or FakeGateway(), llm, memory or FakeMemory(), max_tool_rounds, min_confidence,
        callable_timeout_s,
    )
