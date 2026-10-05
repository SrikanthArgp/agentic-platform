# ADR-0019: `orchestrator` fetches memory context before the agent loop, not as a tool

- **Status**: Accepted
- **Date**: 2026-10-05
- **See**: `ARCHITECTURE.md` §5, §6, §13 (T3); `plan.md` Day 7

## Context

`GetContext` (§6) gives an `alert_key`'s recent history: decision counts
over 1h/24h/7d, analyst verdict counts, and the `is_novel_alert` /
`has_confirmed_incident_history` flags. §6 left open whether
`orchestrator` calls it itself, before the tool-calling loop, or offers it
to the agent as a tool.

The same values are also needed outside the LLM: `escalate_when` rules can
read `context.*` (ADR-0010), and the supervisor lowers confidence on a
novel key (ADR-0020).

## Decision

- `orchestrator` calls `memory-store.GetContext` over gRPC once per run,
  before the first LLM call, for the run's `app_id` + `alert_key`.
- The response goes into the agent's first message as a data block
  (`kind=memory_context`), next to the alert; it's the same object the
  guardrails and supervisor read.
- One `grpc.aio` channel per process, opened at startup and reused for
  every call (`MEMORY_STORE_TARGET`, 1s deadline, `MEMORY_STORE_TIMEOUT_S`).
- If the call fails (unreachable, deadline, any error status), the run
  goes on without context: the prompt says context is unavailable,
  `context.*` guardrails can't match, confidence is capped (ADR-0020) so
  the supervisor escalates, and `reasons[]` says why. Memory being down
  never makes a decision *less* cautious.

## Alternatives considered

- **`GetContext` as an MCP tool the agent may call** — the agent could
  skip it, and then the guardrails and supervisor would have nothing to
  read; it costs an extra LLM round (seconds) for a ~1 ms lookup; and it
  would need `memory-store` behind MCP or proxied through `tool-gateway`,
  a second path to the same data. Tools stay for lookups whose arguments
  the agent chooses (e.g. Day 13's `similar-past-case-lookup`).
- **Fail the run (not evaluated) when memory is down** — also escalates,
  but throws away the agent's own read of the alert, which an analyst can
  still use.
- **A new channel per call** — a TCP + HTTP/2 handshake on every alert for
  no benefit; ruled out by `plan.md` Day 7.

## Consequences

- ✅ Every run has the same inputs for the LLM, supervisor and guardrails;
  the agent can't forget its history.
- ✅ One fewer LLM round trip than the tool approach.
- ❌ Every run pays the `GetContext` call even when the alert type would
  never need history (~1 ms warm, Day 6).
- ❌ The context is fixed at the start of the run; it doesn't reflect
  decisions made while the agent is reasoning (seconds; acceptable).
