# ADR-0002: Kafka as the event backbone, keyed by `{app_id}:{alert_key}`

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §8, §10; ADR-0008

## Context

Alerts arrive in bursts (the security app especially) and each decision
takes seconds of LLM time. The intake must accept alerts instantly and
never lose one; consumers must be able to scale out, restart, and catch up.
Memory recurrence counts are read-then-updated per `alert_key`, so two
consumers working the same key at once would corrupt them.

## Decision

- Kafka carries the four topics: `alert.received`, `alert.decided`,
  `verdict.recorded`, `alert.received.dlq`.
- **Every message is keyed `{app_id}:{alert_key}`** so one key's events
  are ordered on one partition, while different keys spread across
  `orchestrator` replicas. Topics are created with multiple partitions
  (e.g. 12), not the default 1.
- Producers are idempotent; consumers process one message at a time per
  key and commit offsets only after handling.
- Delivery is **at-least-once**; consumers dedupe on `alert_id` (Redis
  `seen:` marker in `orchestrator`, unique constraint on `cases`).

## Alternatives considered

- **Redis Streams** — already running, far lighter to operate, has
  consumer groups. Honestly sufficient for this build's load. Rejected
  mainly because retention/replay and partition-based scaling are weaker
  and less standard, and this is a production-shaped reference build.
- **RabbitMQ** — good work-queue semantics, but no log replay, and
  per-key ordering with competing consumers needs extra design.
- **Direct gRPC from `ingestion` to `orchestrator`** — no buffering; a
  slow LLM back-pressures the alert source.

## Consequences

- ✅ Bursts buffer instead of overloading `orchestrator`; replicas scale to
  the partition count; consumers can replay after a bug fix.
- ✅ Per-key ordering keeps memory counts correct across replicas.
- ❌ Heaviest component in the stack to run, for a load (a few
  alerts/sec, LLM-bound) that doesn't require it. This is the main
  over-engineering cost of the design, accepted knowingly.
- ❌ At-least-once means every consumer must be idempotent.
- ❌ A very noisy single `alert_key` is processed serially on one
  partition — acceptable, since repeats are what memory/`SUPPRESS` handle.

Revisit if the platform stays small: Redis Streams with the same keying
rule is the cheaper fallback.
