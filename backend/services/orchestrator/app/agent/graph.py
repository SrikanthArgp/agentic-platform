"""The run as a LangGraph graph (ADR-0022): the decision order, made explicit.

    START -> context -> agent -> supervisor -> guardrails -> callable x N (parallel) -> fold -> END
                                                         (no callable matches: guardrails -> fold)

- `context`: `memory-store` GetContext once, before the agent (ADR-0019).
  Failure isn't fatal: the run goes on without context, with a reason.
- `agent`: the entry agent's tool-calling loop (`react.py`), then its
  answer parsed. No answer, an unparseable one, or a failed tool lookup ->
  ESCALATE.
- `supervisor`: confidence, capped by fixed rules, below the app's
  threshold -> ESCALATE (`supervisor.py`, ADR-0020).
- `guardrails`: any matching `escalate_when` rule -> ESCALATE, named in a
  reason (`guardrails.py`, ADR-0010/0021).

- `callable` (Day 8, ADR-0012): one branch per callable agent whose
  `invoke_on` contains the *final* decision, fanned out with `Send` so they
  run concurrently. Each runs its own tool loop and returns reasons only;
  a callable that fails or times out (`CALLABLE_TIMEOUT_S`) adds a "didn't
  contribute" reason instead and never blocks the decision.
- `fold`: callables' reasons, each prefixed with its `agent_id`, in
  manifest order.

`supervisor` and `guardrails` only ever move a decision to ESCALATE;
callables never change it. Platform reasons accumulate in
`platform_reasons` in node order, after the agent's own reasons, and the
callables' after those.

`build_callable_graph()` is the direct path for `RunAgent` with a callable
`agent_id` (tests, debugging): `context -> explain -> END`, same code.

Compiled once per process; per-run dependencies (chat model, MCP session,
memory client) come in as the graph's runtime context (`RunDeps`). No
checkpointer: runs are stateless.
"""

import asyncio
import logging
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from opentelemetry import trace

from app.agent import guardrails, prompt
from app.agent.decision import DecisionParseError, parse_decision, parse_explanation
from app.agent.llm import LLMError
from app.agent.react import REASON_PREFIX, RunTools, run_agent
from app.agent.supervisor import supervise
from app.core.manifest import AgentSpec, AppManifest, ResolutionError
from app.memory.client import ContextSource, ContextUnavailableError
from app.tools.gateway import ToolGateway, ToolSession
from proto_gen import agent_pb2, memory_store_pb2
from run_context import RunContext

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class RunDeps:
    model: BaseChatModel
    memory: ContextSource
    # The entry agent's MCP session; each callable opens its own.
    tools: ToolSession | None
    gateway: ToolGateway
    # (manifest, agent) -> the agent's prompt text (`ManifestStore.prompt`).
    prompts: Callable[[AppManifest, AgentSpec], str]
    max_tool_rounds: int
    # Supervisor threshold for apps whose manifest sets none.
    default_min_confidence: float
    callable_timeout_s: float = 30.0


class CallableFailed(RuntimeError):
    """A callable agent produced nothing usable; the message says why."""


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
    # callable branches: (manifest index, reasons), in whatever order they finish.
    callable_results: Annotated[list[tuple[int, list[str]]], operator.add]
    # fold: the callables' reasons, in manifest order.
    delegated_reasons: list[str]


class CallableTask(TypedDict):
    """What `Send` hands one `callable` branch."""

    index: int
    agent: AgentSpec
    request: agent_pb2.RunAgentRequest
    manifest: AppManifest
    memory_context: memory_store_pb2.GetContextResponse | None
    # The entry agent's final decision and reasons, as the callable sees them.
    entry: dict[str, Any]


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


def route_callables(state: RunState) -> list[Send] | str:
    """One `Send` per callable whose `invoke_on` has the final decision."""
    decision = agent_pb2.Decision.Name(state["decision"])
    agents = state["manifest"].callables_for(decision)
    if not agents:
        return "fold"
    entry = {"decision": decision, "reasons": [*state["reasons"], *state["platform_reasons"]]}
    return [
        Send("callable", CallableTask(
            index=i, agent=agent, request=state["request"], manifest=state["manifest"],
            memory_context=state.get("memory_context"), entry=entry,
        ))
        for i, agent in enumerate(agents)
    ]


async def callable_node(task: CallableTask, runtime: Runtime[RunDeps]) -> RunState:
    deps, agent = runtime.context, task["agent"]
    try:
        reasons, _, failures = await asyncio.wait_for(
            explain(deps, task["manifest"], agent, task["request"], task["memory_context"], task["entry"]),
            deps.callable_timeout_s,
        )
        out = [f"{agent.agent_id}: {r}" for r in reasons] + failures
    except Exception as e:  # a callable never blocks the decision
        why = _failure(e, deps)
        if why == "failed":
            logger.exception("callable %s failed", agent.agent_id)
        out = [REASON_PREFIX + f"callable '{agent.agent_id}' didn't contribute ({why})."]
    return {"callable_results": [(task["index"], out)]}


def fold_node(state: RunState) -> RunState:
    ordered = sorted(state.get("callable_results", []), key=lambda r: r[0])
    return {"delegated_reasons": [reason for _, reasons in ordered for reason in reasons]}


async def explain(
    deps: RunDeps,
    manifest: AppManifest,
    agent: AgentSpec,
    request: agent_pb2.RunAgentRequest,
    memory_context: memory_store_pb2.GetContextResponse | None,
    entry: dict[str, Any] | None,
) -> tuple[list[str], list[agent_pb2.ToolCall], list[str]]:
    """Run a callable agent: (its reasons, its tool trace, tool-failure reasons).
    Raises `CallableFailed`, `DecisionParseError` or `LLMError` when it
    produced nothing usable."""
    app_prompt = deps.prompts(manifest, agent)
    run_context = RunContext(app_id=manifest.app_id, agent_id=agent.agent_id, alert_id=request.alert_id)
    nonce = prompt.new_nonce()
    with tracer.start_as_current_span("agent.callable", attributes={"agent_id": agent.agent_id}):
        async with deps.gateway.connect() as session:
            tools = RunTools(agent.tools, session, run_context, nonce)
            outcome = await run_agent(
                deps.model,
                prompt.build_system_prompt(app_prompt, nonce, role="callable"),
                prompt.explain_message(request, memory_context, entry, nonce),
                tools,
                deps.max_tool_rounds,
            )
    if outcome.ran_out:
        raise CallableFailed(f"no answer within {deps.max_tool_rounds} tool-call rounds")
    failures = [
        REASON_PREFIX + f"{agent.agent_id}: " + f.removeprefix(REASON_PREFIX) for f in tools.infra_failures
    ]
    return parse_explanation(outcome.text), tools.trace, failures


def _failure(e: Exception, deps: RunDeps) -> str:
    match e:
        case TimeoutError():
            return f"timed out after {deps.callable_timeout_s:g}s"
        case LLMError():
            return f"LLM unavailable: {e}"
        case DecisionParseError():
            return f"answer didn't parse: {e}"
        case CallableFailed() | ResolutionError():
            return str(e)
        case _:
            return "failed"


async def explain_node(state: RunState, runtime: Runtime[RunDeps]) -> RunState:
    """Direct `RunAgent` on a callable: its reasons unprefixed, no decision."""
    try:
        reasons, trace_calls, failures = await asyncio.wait_for(
            explain(runtime.context, state["manifest"], state["agent"], state["request"],
                    state.get("memory_context"), None),
            runtime.context.callable_timeout_s,
        )
    except (TimeoutError, CallableFailed, DecisionParseError) as e:
        reasons, trace_calls, failures = [REASON_PREFIX + f"no explanation ({_failure(e, runtime.context)})."], [], []
    return {
        "decision": agent_pb2.DECISION_UNSPECIFIED,
        "confidence": 0.0,
        "reasons": reasons,
        "tool_calls": trace_calls,
        "platform_reasons": failures,
    }


def build_graph() -> CompiledStateGraph:
    graph = StateGraph(RunState, context_schema=RunDeps)
    graph.add_node("context", context_node)
    graph.add_node("agent", agent_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("guardrails", guardrails_node)
    graph.add_node("callable", callable_node)
    graph.add_node("fold", fold_node)
    graph.add_edge(START, "context")
    graph.add_edge("context", "agent")
    graph.add_edge("agent", "supervisor")
    graph.add_edge("supervisor", "guardrails")
    graph.add_conditional_edges("guardrails", route_callables, ["callable", "fold"])
    graph.add_edge("callable", "fold")
    graph.add_edge("fold", END)
    return graph.compile(name="entry-run")


def build_callable_graph() -> CompiledStateGraph:
    graph = StateGraph(RunState, context_schema=RunDeps)
    graph.add_node("context", context_node)
    graph.add_node("explain", explain_node)
    graph.add_edge(START, "context")
    graph.add_edge("context", "explain")
    graph.add_edge("explain", END)
    return graph.compile(name="callable-run")
