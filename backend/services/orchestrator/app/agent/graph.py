"""The run as a LangGraph graph (ADR-0022): the decision order, made explicit.

    START -> context -> agent -> supervisor -> guardrails -> END

- `context`: `memory-store` GetContext once, before the agent (ADR-0019).
  Failure isn't fatal: the run goes on without context, with a reason.
- `agent`: the entry agent's tool-calling loop (`react.py`), then its
  answer parsed. No answer, an unparseable one, or a failed tool lookup ->
  ESCALATE.
- `supervisor`: confidence, capped by fixed rules, below the app's
  threshold -> ESCALATE (`supervisor.py`, ADR-0020).
- `guardrails`: any matching `escalate_when` rule -> ESCALATE, named in a
  reason (`guardrails.py`, ADR-0010/0021).

`supervisor` and `guardrails` only ever move a decision to ESCALATE.
Platform reasons accumulate in `platform_reasons` in node order, after the
agent's own reasons.

Compiled once per process; per-run dependencies (chat model, MCP session,
memory client) come in as the graph's runtime context (`RunDeps`). No
checkpointer: runs are stateless.
"""

import operator
from dataclasses import dataclass
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from opentelemetry import trace

from app.agent import guardrails, prompt
from app.agent.decision import DecisionParseError, parse_decision
from app.agent.react import REASON_PREFIX, RunTools, run_agent
from app.agent.supervisor import supervise
from app.core.manifest import AgentSpec, AppManifest
from app.memory.client import ContextSource, ContextUnavailableError
from app.tools.gateway import ToolSession
from proto_gen import agent_pb2, memory_store_pb2
from run_context import RunContext

tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class RunDeps:
    model: BaseChatModel
    memory: ContextSource
    tools: ToolSession
    max_tool_rounds: int
    # Supervisor threshold for apps whose manifest sets none.
    default_min_confidence: float


class RunState(TypedDict, total=False):
    # Inputs.
    request: agent_pb2.RunAgentRequest
    manifest: AppManifest
    agent: AgentSpec
    app_prompt: str
    run_context: RunContext
    # context
    memory_context: memory_store_pb2.GetContextResponse | None
    # agent, then supervisor/guardrails
    decision: int
    confidence: float
    reasons: list[str]
    tool_calls: list[agent_pb2.ToolCall]
    input_tokens: int
    output_tokens: int
    # `orchestrator: ...` reasons, appended by each node in order.
    platform_reasons: Annotated[list[str], operator.add]


async def context_node(state: RunState, runtime: Runtime[RunDeps]) -> RunState:
    request = state["request"]
    with tracer.start_as_current_span("memory.get_context"):
        try:
            ctx = await runtime.context.memory.get_context(state["manifest"].app_id, request.alert_key)
        except ContextUnavailableError as e:
            return {
                "memory_context": None,
                "platform_reasons": [REASON_PREFIX + f"memory context unavailable ({e}); decided without "
                                     "this alert_key's history."],
            }
    return {"memory_context": ctx}


async def agent_node(state: RunState, runtime: Runtime[RunDeps]) -> RunState:
    deps = runtime.context
    nonce = prompt.new_nonce()
    tools = RunTools(state["agent"].tools, deps.tools, state["run_context"], nonce)
    outcome = await run_agent(
        deps.model,
        prompt.build_system_prompt(state["app_prompt"], nonce),
        prompt.alert_message(state["request"], state.get("memory_context"), nonce),
        tools,
        deps.max_tool_rounds,
    )

    confidence = 0.0
    if outcome.ran_out:
        decision = agent_pb2.ESCALATE
        reasons = [REASON_PREFIX + f"agent reached no decision within {deps.max_tool_rounds} tool-call rounds. "
                   "Escalated for human review."]
    else:
        try:
            parsed = parse_decision(outcome.text)
            decision, confidence, reasons = parsed.decision, parsed.confidence, parsed.reasons
        except DecisionParseError as e:
            decision, reasons = agent_pb2.ESCALATE, [REASON_PREFIX + f"{e} Escalated for human review."]

    extra = list(tools.infra_failures)
    if extra and decision != agent_pb2.ESCALATE:
        extra.append(REASON_PREFIX + "escalated because a tool lookup failed; the agent decided "
                     "without that evidence.")
        decision = agent_pb2.ESCALATE
    return {
        "decision": decision,
        "confidence": confidence,
        "reasons": reasons,
        "tool_calls": tools.trace,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "platform_reasons": extra,
    }


def supervisor_node(state: RunState, runtime: Runtime[RunDeps]) -> RunState:
    threshold = state["manifest"].min_confidence(runtime.context.default_min_confidence)
    result = supervise(state["decision"], state["confidence"], state.get("memory_context"), threshold)
    return {
        "decision": result.decision,
        "confidence": result.confidence,
        "platform_reasons": [REASON_PREFIX + result.reason] if result.reason else [],
    }


def guardrails_node(state: RunState) -> RunState:
    before = state["decision"]
    reasons = []
    for match in guardrails.evaluate(state["manifest"].guardrails(), state["request"], state.get("memory_context")):
        if before == agent_pb2.ESCALATE:
            reasons.append(REASON_PREFIX + f"guardrail {match.describe()} matched (already ESCALATE).")
        else:
            reasons.append(REASON_PREFIX + f"guardrail {match.describe()} matched; "
                           f"{agent_pb2.Decision.Name(before)} changed to ESCALATE.")
    decision = agent_pb2.ESCALATE if reasons else before
    return {"decision": decision, "platform_reasons": reasons}


def build_graph() -> CompiledStateGraph:
    graph = StateGraph(RunState, context_schema=RunDeps)
    graph.add_node("context", context_node)
    graph.add_node("agent", agent_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("guardrails", guardrails_node)
    graph.add_edge(START, "context")
    graph.add_edge("context", "agent")
    graph.add_edge("agent", "supervisor")
    graph.add_edge("supervisor", "guardrails")
    graph.add_edge("guardrails", END)
    return graph.compile(name="entry-run")
