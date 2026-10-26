# ADR-0024: Verdicts go through an outbox too

- **Status**: Accepted
- **Date**: 2026-10-26
- **See**: ADR-0008, ADR-0011, ADR-0017; `ARCHITECTURE.md` §8, §9, §10; `plan.md` Days 11, 15

## Context

ADR-0008 gives `ingestion` an outbox, but `review-console` still publishes
`verdict.recorded` inline: under the case's row lock it resolves the case,
publishes, and commits only once Kafka has the event, returning `503` if
the publish fails so the case stays `OPEN` for a retry.

Day 11 found that a `503` from the 10s send *timeout* doesn't mean "not
sent". The idempotent producer keeps the message queued and delivers it
once Kafka returns. Each side then keeps the first verdict it sees:
`cases` resolves only once, and `memory_events` is unique per `case_id`
(ADR-0017). So:

1. The analyst submits `CONFIRMED_INCIDENT` during an outage: `503`, the
   case stays `OPEN`, and the event is still queued.
2. They retry with `CONFIRMED_NOISE`, and the case resolves as noise.
3. Kafka returns: `memory-store` records `INCIDENT` and drops `NOISE` as a
   duplicate.

The case says noise and memory says incident, permanently and silently.
That memory feeds `has_confirmed_incident_history` and the confirmed
counts, which the supervisor and agents use on every later alert for that
key, so it corrupts the feedback loop the platform exists for.

## Decision

- **`review-console` writes each verdict to its own `verdict_outbox`
  table in the transaction that resolves the case**, and never publishes
  to Kafka directly. The case is `RESOLVED` with an outbox row, or
  neither. `POST /cases/{id}/verdict` no longer depends on Kafka: it
  returns `200` during a Kafka outage, and a `503` now means only that
  Postgres was unavailable, with nothing committed.
- **`verdict_outbox` has the same relay columns as `outbox`** (`id`,
  `app_id`, `case_id`, `topic`, `kafka_key` = `{app_id}:{alert_key}`,
  `payload` (bytea: the same `verdict.recorded` JSON as today), `headers`,
  `status`, `attempts`, `last_error`, `created_at`, `sent_at`). It's
  owned by `review-console`, as `cases` is, rather than sharing
  `ingestion`'s table.
- **The ADR-0008 relay drains both tables**: the same Celery task, the
  same Redis lock, `id` order per table, and the same pruning of `SENT`
  rows. The relay is written once, over a table and its topic.
- **The relay publishes each verdict with its row's `created_at` as the
  Kafka message timestamp.** `memory-store` times a verdict by its Kafka
  timestamp, which was the moment of the verdict only while publishing
  was inline. Without this, a verdict held through an outage would land
  in the rolling windows at relay time.
- **Failures follow ADR-0008**: Kafka unreachable → rows stay `PENDING`
  with no attempts counted. A message-specific failure counts an attempt,
  and after 5 the row is `DEAD`, logged at `ERROR` and kept. There's no
  `verdict.recorded.dlq` topic: the payload is small JSON the platform
  builds itself, so a rejected one is a bug, and the `DEAD` row is
  already the durable record for inspection.
- `memory-store` doesn't change. A relay redelivery has the same
  `case_id` and is stored once.

## Alternatives considered

- **Accept the issue and document it.** Rare, but silent and permanent,
  and it lands in the data later decisions on that key read.
- **Purge or expire the queued message on timeout.** Racy: a message may
  already be on its way to the broker when the client gives up, so the
  outcome is still unknown.
- **`memory-store` keeps the last verdict per case instead of the
  first.** Correct only if the two arrive in order, which nothing
  guarantees, and it breaks the event log's stored-once rule (ADR-0017).
- **Mark the case "publish pending" on a `503` and reconcile later.** An
  outbox without the relay; it still needs a background job to finish
  it.
- **One shared `outbox` table for both services.** Fewer tables, but
  `review-console` would write to a table whose shape `ingestion` owns
  (its rows carry an `alert_id` and a protobuf payload). Two tables with
  the same relay columns let one relay serve both.

## Consequences

- ✅ `cases` and `memory_events` can't disagree about a verdict.
- ✅ Analysts can record verdicts while Kafka is down.
- ❌ Up to ~1s before `memory-store` sees a verdict (the relay interval),
  and longer during an outage. The next alert on that key may be decided
  before the verdict counts.
- ❌ One more table, and the relay now serves two producers.
