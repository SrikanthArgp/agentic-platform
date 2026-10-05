# ADR-0017: Memory stored as an event log; rolling windows counted exactly

- **Status**: Accepted
- **Date**: 2026-10-05
- **See**: `ARCHITECTURE.md` §6, §8; `plan.md` Days 6 and 10

## Context

`ARCHITECTURE.md` §8 planned `memory_history` as a snapshot of each
window's counts on every update, with Redis rebuilt from it on a miss. A
rolling-window count can't be rebuilt from a snapshot: "5 alerts in the
last hour, as of 10:00" says nothing about the last hour at 10:30. Counters
TTL'd per window (`ctx:…:{window}`) have the same problem: they expire all
at once instead of sliding.

## Decision

- **Postgres `memory_events`** is the source of truth: one row per event
  (`app_id`, `alert_key`, `kind`, `ref_id`, `occurred_at`). `kind` is a
  decision (`decision:ESCALATE`, `decision:SUPPRESS`,
  `decision:AUTO_RESOLVE`, `decision:DECISION_UNSPECIFIED`) or, from Day 10,
  a verdict. Unique on (`app_id`, `kind` family, `ref_id`), so a redelivered
  event is stored once.
- **Redis** holds a cache per `{memory_namespace}:{alert_key}`:
  - a sorted set of the last 7 days of events, scored by time, trimmed on
    every write and read;
  - a hash of all-time facts (`seen`, `confirmed_incident`) plus a
    `loaded` marker, TTL'd so idle keys expire and are rebuilt on demand.
- **Window counts are computed by one pure function** over the 7-day event
  list, with windows `(now − w, now]`. The Redis read and the Postgres
  rebuild both feed it, so they can't disagree.
- **Miss**: no `loaded` marker → read the last 7 days plus the all-time
  facts from Postgres, write them to Redis, set `loaded`. Writes always go
  to Postgres first, then are added to Redis (idempotently), so a rebuild
  racing a write can't drop the write.
- `memory_history` is removed from the schema (it never had a writer).

## Alternatives considered

- **Per-window snapshots (the original plan)** — can't rebuild a sliding
  window.
- **Counters TTL'd per window** — tumbling, not rolling: the 1h count
  drops to 0 at once instead of sliding.
- **Time-bucketed counters (e.g. per minute)** — approximate at the window
  edge and more keys; event volume per `alert_key` is small enough to keep
  exact events.
- **Postgres only, no Redis** — a `COUNT` per window per call works at this
  scale, but misses Day 6's "low single-digit ms" target under load and
  drops the documented hot-path design.

## Consequences

- ✅ Exact rolling windows, and a rebuild that gives the same answer as the
  cache.
- ✅ Day 10 verdicts are just another event kind.
- ❌ Postgres grows by one row per decision; a retention job (e.g. prune
  older than 90 days, keeping all-time facts) is needed before real
  volumes — not in this build.
- ❌ A hot `alert_key` holds up to 7 days of events in one sorted set;
  fine at triage volumes, would need bucketing for very high rates.
