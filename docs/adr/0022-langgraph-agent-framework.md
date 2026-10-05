# ADR-0022: LangGraph for `orchestrator`'s control flow; LangChain's agent inside it

- **Status**: Accepted
- **Date**: 2026-10-05
- **See**: ADR-0015 (its LLM interface is superseded here; its provider choice stands), ADR-0010, ADR-0019, ADR-0020; `ARCHITECTURE.md` §5, §13 (T1, T2, T4); `plan.md` Days 7–8

## Context

Through Day 7, `orchestrator`'s agent run was ~200 lines of our own code:
an `LLMClient` Protocol with an OpenAI adapter (ADR-0015), a hand-written
tool-calling loop, then the supervisor and guardrails as plain function
calls. Day 8 adds callable agents that run in parallel after the decision.
The project wants a mainstream agent framework so the agent layer is
familiar to people who already know one and can use its ecosystem
(middleware, tracing integrations, more providers) later.

The run has properties a framework must not loosen:

- the decision order is fixed: agent, supervisor, guardrails, callables
  (§5); the last three are deterministic code;
- `app_id`/`agent_id`/`alert_id` reach `tool-gateway` in MCP `_meta`,
  never as tool arguments (§13 T4);
- tool results enter the prompt only inside nonce-delimited data blocks
  (§13 T1/T2);
- the agent is offered, and may call, only its `registry`-resolved tools;
- every failure ends in ESCALATE (§13.4).

## Decision

- **Outer graph: a LangGraph `StateGraph`**, compiled once per process:
  `context → agent → supervisor → guardrails → (callables, Day 8) → END`.
  Each node is a small function over the run's state; the supervisor and
  guardrails are the existing pure modules (ADR-0020, ADR-0010/0021).
  Per-run dependencies (LLM, MCP session, memory client) are passed as the
  graph's runtime context. No checkpointer: runs are stateless (§4).
- **The `agent` node runs LangChain's prebuilt agent**
  (`langchain.agents.create_agent`, the LangGraph 1.x successor of
  `create_react_agent`), built per run (~1 ms) because each agent has its
  own tools:
  - **Model**: a LangChain chat model (`langchain-openai`'s `ChatOpenAI`,
    `gpt-5.4-mini` default, 60s timeout, 2 retries). This replaces our
    `LLMClient` Protocol and OpenAI adapter; LangChain's chat-model
    interface is the provider seam now. Any model-call failure becomes
    `LLMError` (middleware), so callers still see one error type.
  - **Tools**: our own `StructuredTool` per resolved tool, wrapping the
    existing MCP `ToolSession`. They send the run context in `_meta`,
    return the result already inside a data block, and record the tool
    trace and infrastructure failures. Not `langchain-mcp-adapters`: we
    need the `_meta` run context and the data-block wrapping, which it
    doesn't give us.
  - **Middleware**: a call to a tool the agent wasn't offered is answered
    `tool_not_allowed` without reaching `tool-gateway`; after
    `MAX_TOOL_ROUNDS` rounds of tool calls the agent stops, and the run
    escalates.
  - The final answer is still JSON parsed by `parse_decision`, not
    LangChain's structured output, which would add an LLM call per run.
- **Tests** use a scripted LangChain fake chat model (a `BaseChatModel`
  subclass that records each call and the tools bound to it). No test
  calls a real LLM.
- Pinned in `orchestrator` only: `langgraph`, `langchain`,
  `langchain-core`, `langchain-openai`.

## Alternatives considered

- **Keep our own loop** (the Day 3–7 design) — smallest code and
  dependency surface; rejected for the reasons in Context.
- **LangGraph for control flow only, our own LLM interface and loop
  inside** — keeps ADR-0015's interface, but then the framework does
  little; we'd still own the loop.
- **`langchain-mcp-adapters` for tools** — less code, but no way to put the
  run context in `_meta` per call, so `app_id` would have to be a tool
  argument or a connection-level header, reopening §13 T4.
- **LangChain structured output for the decision** — an extra model call
  (or provider-specific JSON mode) per run, for a format `parse_decision`
  already validates strictly.

## Consequences

- ✅ The agent layer is a standard LangGraph/LangChain shape; Day 8's
  callables are a graph fan-out, and later providers are a chat-model
  swap.
- ✅ The security properties above are kept by our tool wrapper and
  middleware, and tested.
- ❌ Four more dependencies with a history of API moves
  (`create_react_agent` was deprecated for `create_agent` in 1.x); upgrades
  need the tests and a read of the changelog.
- ❌ An LLM reply whose tool-call arguments aren't valid JSON is no longer
  sent back to the agent as `invalid_arguments`: LangChain sets it aside
  as an invalid tool call, the agent sees no tool call, and the run ends
  without a parseable answer, so it escalates. Safe, but one retry less.
