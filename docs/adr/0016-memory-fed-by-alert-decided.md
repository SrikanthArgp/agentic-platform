# ADR-0016: `memory-store` counts decisions by consuming `alert.decided`; `RunAgentResponse` carries `alert_key`

- **Status**: Accepted
- **Date**: 2026-10-05
- **See**: `ARCHITECTURE.md` §4, §5, §6, §8; `plan.md` Day 6

## Context

`GetContextResponse` reports, per `alert_key`, how often the platform
decided ESCALATE or SUPPRESS over 1h/24h/7d. Day 6 must produce those
counts, but nothing sent decisions to `memory-store`: the event flow only
had it consuming `verdict.recorded` (Day 10). Separately, `RunAgentResponse`
had no `alert_key`, so any consumer of `alert.decided` had to parse it out
of the Kafka message key, and `review-console` (Day 9) needs it on every
`cases` row.

## Decision

- `memory-store` consumes `alert.decided` in its own consumer group
  (`memory-store`), alongside `review-console`'s. Each entry-agent decision
  becomes one decision event for that `app_id` + `alert_key`.
- `RunAgentResponse` gains `string alert_key = 7`, echoed by `orchestrator`
  from the request (including "not evaluated" responses).
- Events are idempotent by `alert_id`: a redelivered `alert.decided` (§10)
  is counted once.
- A decision's time is the Kafka message timestamp (when `orchestrator`
  published it).

## Alternatives considered

- **`orchestrator` calls a `RecordDecision` gRPC on `memory-store`** — a
  second synchronous dependency on the hot path, and a second way decisions
  leave `orchestrator`; a failed call would have to be retried by hand.
- **Parse `alert_key` from the Kafka key** (`{app_id}:{alert_key}`) — works
  because `app_id` is a slug without `:`, but makes the key format a payload
  contract. Every consumer would repeat that parsing.
- **`orchestrator` writes counts to Redis itself** — two writers to one
  store, and `memory-store` would no longer own its data.

## Consequences

- ✅ Decision counts follow from the existing event backbone; per-key
  ordering (ADR-0002) keeps them correct across replicas.
- ✅ `alert_key` is on the decision itself, for `memory-store`,
  and `review-console`.
- ❌ Counts lag the decision by Kafka delivery time (milliseconds normally;
  longer if `memory-store` is down, then caught up from its offset).
- ❌ A proto change: stubs are regenerated, and old messages have an empty
  `alert_key` (none exist outside dev).
