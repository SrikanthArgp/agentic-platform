"""The agent run: `RunAgentRequest` -> `RunAgentResponse` (docs/ARCHITECTURE.md §5).

App-agnostic: everything app-specific (prompt, tools, guardrails,
threshold, callables) comes from the app's manifest. The run itself is a
LangGraph graph (`graph.py`, ADR-0022): memory context, the agent's
tool-calling loop (`react.py`), supervisor, guardrails, then the callable
agents whose `invoke_on` matches the final decision, in parallel, their
reasons folded in. `agent_id` picks the path: empty -> the entry agent and
that whole graph; a callable's id -> just that callable (reasons only, no
decision), for tests and debugging. Failure always
moves toward ESCALATE, never away from it (§13.4):

- the agent's final answer doesn't parse, or it never stops calling tools
  -> ESCALATE, with a reason saying why;
- a tool failed for an infrastructure reason -> ESCALATE, whatever the
  agent concluded;
- memory-store is down -> the run goes on without context, and the
  supervisor caps confidence so it escalates;
- a callable that fails or times out -> a "didn't contribute" reason, the
  decision unchanged;
- anything that stops the run (LLM down, unknown app) raises; callers turn
  that into `not_evaluated_response()`.

The agent is offered exactly the tools `registry` resolved for it
(`AgentSpec.tools`: allowlisted, declared, enabled); a call for anything
else is refused before `tool-gateway`, which checks the same list again.

Exactly one response per run: callables' own answers are never returned
or published on their own.

Not yet: budgets (Day 14).
"""

import logging

from langchain_core.language_models import BaseChatModel
from opentelemetry import trace

from app.agent.graph import RunDeps, build_callable_graph, build_graph
from app.agent.react import REASON_PREFIX
from app.core.manifest import ManifestStore
from app.memory.client import ContextSource
from app.tools.gateway import ToolGateway
from proto_gen import agent_pb2
from run_context import RunContext

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

__all__ = ["AgentRunner", "REASON_PREFIX", "not_evaluated_response"]


class AgentRunner:
    def __init__(
        self,
        manifests: ManifestStore,
        gateway: ToolGateway,
        model: BaseChatModel,
        memory: ContextSource,
        max_tool_rounds: int = 5,
        min_confidence: float = 0.6,
        callable_timeout_s: float = 30.0,
    ):
        self._manifests = manifests
        self._gateway = gateway
        self._model = model
        self._memory = memory
        self._max_tool_rounds = max_tool_rounds
        self._min_confidence = min_confidence
        self._callable_timeout_s = callable_timeout_s
        self._graph = build_graph()
        self._callable_graph = build_callable_graph()

    async def run(self, request: agent_pb2.RunAgentRequest) -> agent_pb2.RunAgentResponse:
        manifest = await self._manifests.get(request.app_id)
        agent = manifest.agent(request.agent_id)
        app_prompt = self._manifests.prompt(manifest, agent)
        context = RunContext(app_id=manifest.app_id, agent_id=agent.agent_id, alert_id=request.alert_id)

        with tracer.start_as_current_span(
            "agent.run",
            attributes={"app_id": context.app_id, "agent_id": context.agent_id, "alert_id": context.alert_id},
        ):
            async with self._gateway.connect() as tools:
                deps = RunDeps(
                    model=self._model,
                    memory=self._memory,
                    tools=tools,
                    gateway=self._gateway,
                    prompts=self._manifests.prompt,
                    max_tool_rounds=self._max_tool_rounds,
                    default_min_confidence=self._min_confidence,
                    callable_timeout_s=self._callable_timeout_s,
                )
                graph = self._callable_graph if agent.role == "callable" else self._graph
                state = await graph.ainvoke(
                    {
                        "request": request,
                        "manifest": manifest,
                        "agent": agent,
                        "app_prompt": app_prompt,
                        "run_context": context,
                        "platform_reasons": [],
                    },
                    context=deps,
                )

        response = agent_pb2.RunAgentResponse(
            app_id=context.app_id,
            agent_id=context.agent_id,
            alert_id=context.alert_id,
            alert_key=request.alert_key,
            decision=state["decision"],
            reasons=[*state["reasons"], *state["platform_reasons"], *state.get("delegated_reasons", [])],
            tool_calls=state["tool_calls"],
            confidence=state["confidence"],
        )
        logger.info(
            "agent decided: app_id=%s agent_id=%s alert_id=%s decision=%s confidence=%.2f tool_calls=%d "
            "callables=%d input_tokens=%d output_tokens=%d",
            context.app_id, context.agent_id, context.alert_id, agent_pb2.Decision.Name(response.decision),
            response.confidence, len(response.tool_calls), len(state.get("callable_results", [])),
            state.get("input_tokens", 0), state.get("output_tokens", 0),
        )
        return response


def not_evaluated_response(request: agent_pb2.RunAgentRequest, why: str) -> agent_pb2.RunAgentResponse:
    """The decision when a run couldn't happen at all: a human must look."""
    return agent_pb2.RunAgentResponse(
        app_id=request.app_id,
        agent_id=request.agent_id,
        alert_id=request.alert_id,
        alert_key=request.alert_key,
        decision=agent_pb2.ESCALATE,
        reasons=[REASON_PREFIX + f"not evaluated: {why}. Escalated for human review."],
    )
