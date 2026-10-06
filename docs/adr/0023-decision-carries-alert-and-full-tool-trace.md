# ADR-0023: `alert.decided` carries the alert and every agent's tool calls

- **Status**: Accepted
- **Date**: 2026-10-06
- **See**: ADR-0012, ADR-0016, ADR-0011; `ARCHITECTURE.md` §5, §8, §13 (T1, T2); `plan.md` Days 8–9

## Context

Day 9's `review-console` builds a case from each escalated
`RunAgentResponse`, and found two gaps:

- **The alert itself wasn't there.** `RunAgentResponse` carried the
  decision, reasons, tool calls and confidence, but not the alert it was
  about (source, severity, message, time, payload). An analyst could see
  the alert only as the agent paraphrased it in `reasons[]`.
- **Callables' tool calls were dropped.** Day 8 folded each callable's
  reasons into the response but kept `tool_calls[]` as the entry agent's
  trace only, so a `root-cause-summarizer` claim citing
  `recent-changes-lookup` had no matching tool call on the case.

## Decision

- **`RunAgentResponse.alert` (field 9) is the `RunAgentRequest`**, echoed
  unchanged by `orchestrator` on every response it builds, including
  `not_evaluated_response()`. One message type, no second copy of the
  envelope fields to keep in sync. It is untrusted data, as the payload
  always is: `review-console` shows it to the analyst, and nothing puts
  it in a prompt.
- **`ToolCall.agent_id` (field 3)** says which agent made the call.
  `tool_calls[]` is now the entry agent's calls, then each contributing
  callable's, in manifest order (the same order as their folded reasons).
  A callable that didn't contribute (failed, timed out) adds no calls:
  its trace is lost with it, and its "didn't contribute" reason says so.
- `review-console` stores both on the case: the alert as a jsonb `alert`
  column (`{source, severity, message, fired_at, payload}`, `NULL` for a
  decision published before this ADR) and `agent_id` inside each
  `tool_calls` entry.

## Alternatives considered

- **Flat envelope fields on `RunAgentResponse`** (`severity`, `message`,
  ...) — duplicates part of `RunAgentRequest`, and each new envelope field
  would need adding twice.
- **`review-console` also consumes `alert.received`** and joins it to
  `alert.decided` by `alert_id` — a second consumer, a join with no
  ordering guarantee between topics, and a place to store unmatched
  halves.
- **`review-console` asks `ingestion` for the alert** — `ingestion`
  doesn't persist alerts, and a REST call per case couples the two.
- **A separate `delegated_tool_calls` field** — the same data in two
  places; `agent_id` on each call answers "whose" with one list.

## Consequences

- ✅ A case shows what fired, not just what the agent said about it, and
  every tool call behind every reason.
- ✅ Additive protobuf fields: old consumers ignore them, and old messages
  decode with `alert` unset and `agent_id` empty.
- ❌ `alert.decided` messages grow by the size of the alert (payload is
  capped at 32 KB by `ingestion`).
- ❌ The payload now sits in a second topic and in `cases`, so the same
  retention and access concerns apply there (§13).
