"""One agent's tool-calling loop: LangChain's prebuilt agent (ADR-0022).

`create_agent` runs the loop (model -> tool calls -> model ...); what is
platform-specific is around it:

- `RunTools`: one `StructuredTool` per tool `registry` resolved for the
  agent, each wrapping the run's MCP `ToolSession`. A call sends the run
  context in MCP `_meta` (never as an argument, §13 T4), comes back to
  the model inside a data block (§13 T1/T2), and is recorded in the trace.
  Infrastructure failures (`tool_failed`, `registry_unavailable`,
  `gateway_unreachable`) are collected: the caller escalates on them.
- Middleware: a call to a tool the agent wasn't offered is answered
  `tool_not_allowed` and never reaches `tool-gateway`; after
  `max_tool_rounds` rounds of tool calls the loop stops (`ran_out`); any
  model failure is `LLMError`.

The agent is built per run (~1 ms): each agent has its own tools, bound to
this run's session.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, after_model, wrap_tool_call
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from opentelemetry import trace

from app.agent import prompt
from app.agent.llm import ModelErrors
from app.core.manifest import AgentTool
from app.tools.gateway import GATEWAY_UNREACHABLE, ToolResult, ToolSession
from proto_gen import agent_pb2
from run_context import RunContext

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Tool failures that mean "we couldn't look", not "the agent asked wrong":
# the agent's decision can't be trusted, so the run escalates.
INFRA_TOOL_ERRORS = frozenset({"tool_failed", "registry_unavailable", GATEWAY_UNREACHABLE})
TOOL_NOT_ALLOWED = "tool_not_allowed"
RESULT_SUMMARY_CHARS = 300
REASON_PREFIX = "orchestrator: "


class RunTools:
    """The agent's tools for one run, and what they record."""

    def __init__(self, specs: Sequence[AgentTool], session: ToolSession, context: RunContext, nonce: str):
        self._session = session
        self._context = context
        self._nonce = nonce
        self.trace: list[agent_pb2.ToolCall] = []
        self.infra_failures: list[str] = []
        self.tools = [self._tool(spec) for spec in specs]

    def _tool(self, spec: AgentTool) -> StructuredTool:
        async def call(**arguments: Any) -> str:
            return await self.call(spec.tool_id, arguments)

        return StructuredTool.from_function(
            coroutine=call, name=spec.tool_id, description=spec.description, args_schema=spec.input_schema
        )

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        with tracer.start_as_current_span("agent.tool_call", attributes={"tool_name": name}):
            result = await self._session.call_tool(name, arguments, context=self._context)
        return self.record(name, result)

    def refuse(self, name: str) -> str:
        """A tool the agent wasn't offered: never reaches tool-gateway."""
        return self.record(name, ToolResult(True, {"error": TOOL_NOT_ALLOWED, "tool_id": name}))

    def record(self, name: str, result: ToolResult) -> str:
        self.trace.append(
            agent_pb2.ToolCall(tool_name=name, result_summary=summarize(result), agent_id=self._context.agent_id)
        )
        if result.error_code in INFRA_TOOL_ERRORS:
            self.infra_failures.append(REASON_PREFIX + f"tool '{name}' failed ({result.error_code}).")
        return prompt.tool_result_message(name, result.content, self._nonce)

    def middleware(self, max_tool_rounds: int) -> list[AgentMiddleware]:
        offered = {t.name for t in self.tools}

        @wrap_tool_call
        async def only_offered_tools(request, handler):
            name = request.tool_call["name"]
            if name not in offered:
                return ToolMessage(content=self.refuse(name), tool_call_id=request.tool_call["id"], name=name)
            return await handler(request)

        @after_model(can_jump_to=["end"])
        def tool_round_limit(state, runtime):
            # Every AIMessage is one model call; the last one asking for
            # tools once `max_tool_rounds` have run ends the loop.
            calls = sum(isinstance(m, AIMessage) for m in state["messages"])
            last = state["messages"][-1]
            if isinstance(last, AIMessage) and last.tool_calls and calls > max_tool_rounds:
                return {"jump_to": "end"}
            return None

        return [only_offered_tools, tool_round_limit]


@dataclass
class AgentOutcome:
    # The final answer's text; None if the loop stopped on a tool call.
    text: str | None
    ran_out: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    messages: list[Any] = field(default_factory=list)


async def run_agent(
    model: BaseChatModel, system: str, first_message: str, tools: RunTools, max_tool_rounds: int
) -> AgentOutcome:
    agent = create_agent(
        model,
        tools.tools,
        system_prompt=system,
        middleware=[ModelErrors(), *tools.middleware(max_tool_rounds)],
    )
    # Each round is a model call and a tool call (+ middleware steps); the
    # middleware stops the loop long before LangGraph's own limit.
    out = await agent.ainvoke(
        {"messages": [HumanMessage(first_message)]}, {"recursion_limit": 10 * (max_tool_rounds + 2)}
    )
    messages = out["messages"]
    replies = [m for m in messages if isinstance(m, AIMessage)]
    last = replies[-1] if replies else None
    usage = [m.usage_metadata or {} for m in replies]
    return AgentOutcome(
        text=None if last is None or last.tool_calls else last.text,
        ran_out=bool(last is not None and last.tool_calls),
        input_tokens=sum(u.get("input_tokens", 0) for u in usage),
        output_tokens=sum(u.get("output_tokens", 0) for u in usage),
        messages=messages,
    )


def summarize(result: ToolResult) -> str:
    if result.is_error:
        return f"error: {result.error_code}"
    text = json.dumps(result.content, separators=(",", ":"), sort_keys=True, default=str)
    return text if len(text) <= RESULT_SUMMARY_CHARS else text[: RESULT_SUMMARY_CHARS - 3] + "..."
