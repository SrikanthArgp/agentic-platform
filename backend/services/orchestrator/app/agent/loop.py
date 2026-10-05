"""The agent run: `RunAgentRequest` -> tool-calling loop -> `RunAgentResponse`.

App-agnostic: everything app-specific (prompt, which tools) comes from the
app's manifest, and everything the alert says stays inside data blocks
(`prompt.py`). Failure always moves toward ESCALATE, never away from it
(docs/ARCHITECTURE.md §13.4):

- the agent's final answer doesn't parse, or it never stops calling tools
  -> ESCALATE, with a reason saying why;
- a tool failed for an infrastructure reason (`tool_failed`,
  `gateway_unreachable`) -> ESCALATE, whatever the agent concluded;
- anything that stops the run (LLM down, unknown app) raises; callers turn
  that into `not_evaluated_response()`.

The agent is offered exactly the tools `registry` resolved for it
(`AgentSpec.tools`: allowlisted, declared, enabled); a call for anything
else is refused here, and `tool-gateway` checks the same list again.

Not yet: memory context (Day 7), supervisor + `escalate_when` guardrails
(Day 7), callable agents (Day 8), budgets (Day 14).
"""

import json
import logging

from opentelemetry import trace

from app.agent import prompt
from app.agent.decision import DecisionParseError, parse_decision
from app.agent.llm import (
    AssistantMessage,
    LLMClient,
    Message,
    ToolCallRequest,
    ToolDefinition,
    ToolResultMessage,
    UserMessage,
)
from app.core.manifest import AgentSpec, AppManifest, ManifestStore
from app.tools.gateway import GATEWAY_UNREACHABLE, ToolGateway, ToolResult, ToolSession
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


class AgentRunner:
    def __init__(
        self,
        manifests: ManifestStore,
        gateway: ToolGateway,
        llm: LLMClient,
        max_tool_rounds: int = 5,
    ):
        self._manifests = manifests
        self._gateway = gateway
        self._llm = llm
        self._max_tool_rounds = max_tool_rounds

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
                return await self._run(request, manifest, agent, app_prompt, context, tools)

    async def _run(
        self,
        request: agent_pb2.RunAgentRequest,
        manifest: AppManifest,
        agent: AgentSpec,
        app_prompt: str,
        context: RunContext,
        tools: ToolSession,
    ) -> agent_pb2.RunAgentResponse:
        definitions = [ToolDefinition(t.tool_id, t.description, t.input_schema) for t in agent.tools]
        allowed = {t.tool_id for t in agent.tools}

        nonce = prompt.new_nonce()
        system = prompt.build_system_prompt(app_prompt, nonce)
        messages: list[Message] = [UserMessage(prompt.alert_message(request, nonce))]
        trace_calls: list[agent_pb2.ToolCall] = []
        infra_failures: list[str] = []
        input_tokens = output_tokens = 0

        def respond(decision: agent_pb2.Decision, reasons: list[str]) -> agent_pb2.RunAgentResponse:
            extra = list(infra_failures)
            if infra_failures and decision != agent_pb2.ESCALATE:
                extra.append(REASON_PREFIX + "escalated because a tool lookup failed; the agent decided "
                             "without that evidence.")
                decision = agent_pb2.ESCALATE
            logger.info(
                "agent decided: app_id=%s agent_id=%s alert_id=%s decision=%s tool_calls=%d "
                "input_tokens=%d output_tokens=%d",
                context.app_id, context.agent_id, context.alert_id, agent_pb2.Decision.Name(decision),
                len(trace_calls), input_tokens, output_tokens,
            )
            return agent_pb2.RunAgentResponse(
                app_id=context.app_id,
                agent_id=context.agent_id,
                alert_id=context.alert_id,
                decision=decision,
                reasons=[*reasons, *extra],
                tool_calls=trace_calls,
            )

        for round_ in range(self._max_tool_rounds + 1):
            response = await self._llm.complete(system=system, messages=messages, tools=definitions)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens

            if not response.tool_calls:
                try:
                    parsed = parse_decision(response.text)
                except DecisionParseError as e:
                    return respond(agent_pb2.ESCALATE, [REASON_PREFIX + f"{e} Escalated for human review."])
                return respond(parsed.decision, parsed.reasons)
            if round_ == self._max_tool_rounds:
                break

            messages.append(AssistantMessage(response.text, response.tool_calls))
            for call in response.tool_calls:
                result = await self._call_tool(tools, call, allowed, context)
                trace_calls.append(agent_pb2.ToolCall(tool_name=call.name, result_summary=summarize(result)))
                if result.error_code in INFRA_TOOL_ERRORS:
                    infra_failures.append(REASON_PREFIX + f"tool '{call.name}' failed ({result.error_code}).")
                messages.append(
                    ToolResultMessage(call.id, prompt.tool_result_message(call.name, result.content, nonce))
                )

        return respond(
            agent_pb2.ESCALATE,
            [REASON_PREFIX + f"agent reached no decision within {self._max_tool_rounds} tool-call rounds. "
             "Escalated for human review."],
        )

    async def _call_tool(
        self, tools: ToolSession, call: ToolCallRequest, allowed: set[str], context: RunContext
    ) -> ToolResult:
        if call.name not in allowed:
            # Never reaches tool-gateway: the agent can only call what its
            # manifest allowlists, whatever the model asked for.
            return ToolResult(True, {"error": TOOL_NOT_ALLOWED, "tool_id": call.name})
        if call.arguments is None:
            return ToolResult(True, {"error": "invalid_arguments", "message": "Arguments must be a JSON object."})
        with tracer.start_as_current_span("agent.tool_call", attributes={"tool_name": call.name}):
            return await tools.call_tool(call.name, call.arguments, context=context)


def summarize(result: ToolResult) -> str:
    if result.is_error:
        return f"error: {result.error_code}"
    text = json.dumps(result.content, separators=(",", ":"), sort_keys=True, default=str)
    return text if len(text) <= RESULT_SUMMARY_CHARS else text[: RESULT_SUMMARY_CHARS - 3] + "..."


def not_evaluated_response(request: agent_pb2.RunAgentRequest, why: str) -> agent_pb2.RunAgentResponse:
    """The decision when a run couldn't happen at all: a human must look."""
    return agent_pb2.RunAgentResponse(
        app_id=request.app_id,
        agent_id=request.agent_id,
        alert_id=request.alert_id,
        decision=agent_pb2.ESCALATE,
        reasons=[REASON_PREFIX + f"not evaluated: {why}. Escalated for human review."],
    )
