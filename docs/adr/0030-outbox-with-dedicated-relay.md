# ADR-0030: Transactional outbox with a dedicated `outbox-relay` worker

- **Status**: Accepted
- **Date**: 2026-10-31
- **See**: `ARCHITECTURE.md` §4, §8, §10, §12; ADR-0002, ADR-0014, ADR-0024; `plan.md` Days 15, 16, 18, 25

Folds in and replaces ADR-0008 (outbox with a Celery relay), which is
deleted. The outbox itself stands as ADR-0008 decided it. What changes is
the process that relays it.

## Context

`ingestion` returns `202` before a decision exists. If it then fails to
publish to Kafka (broker down, rejected message), the alert is lost after
the source was told it was accepted. ADR-0024 found the same gap for
`review-console`'s verdicts. Both need a durable write, then a relay to
Kafka that keeps one key's messages in order.

ADR-0008 made that relay a Celery task: `celery-beat` fired it about every
second, Redis was the broker, and a Redis lock stopped two runs from
overlapping. Before building it on Day 15, that turned out to be a poor fit:

- **The job is a loop, not a task.** Celery is built for discrete jobs.
  A relay that runs every second needs a lock to stop a slow run
  overlapping the next tick and reordering a key's messages, plus exactly
  one `celery-beat` everywhere it's deployed. A single long-running loop
  can't overlap with itself.
- **Sync worker, async clients.** Every service uses `aiokafka` and
  `asyncpg`. A Celery task is sync, so it would either start an event loop
  and a new Kafka producer each tick, or bring in a second, sync Kafka
  client just for the relay.
- **Three moving parts for one job**: `celery-worker`, `celery-beat` and a
  Redis broker DB, plus the lock. The other reason ADR-0008 gave for Celery
  was "so Celery has a clear job beyond evals". The nightly eval (Day 25)
  doesn't need it either.

## Decision

- **The outbox stays as ADR-0008 and ADR-0024 decided.** `ingestion`
  writes each validated event to Postgres `outbox` (with its `kafka_key`
  and trace headers) and returns `202`, and never publishes directly.
  `review-console` writes each verdict to `verdict_outbox` in the
  transaction that resolves the case.
- **One dedicated `outbox-relay` worker relays both tables.** It's a
  small asyncio process (`backend/workers/outbox-relay/`, its own `uv`
  project on `ap-shared`, its own image) that reuses `asyncpg` and
  `aiokafka`. It's a worker, not a seventh platform service: it has no API
  beyond `/healthz` and `/readyz`, and it lives outside both services'
  request paths. The relay code is written once, over a table and its
  topic.
- **The loop**: claim a batch of `PENDING` rows in `id` order
  (`FOR UPDATE SKIP LOCKED`), publish each, in order, to its `topic` keyed
  by its stored `kafka_key` with its stored headers, and mark them `SENT`.
  Verdicts are published with their row's `created_at` as the Kafka
  timestamp (ADR-0024). A full batch loops straight away. An empty one
  sleeps `RELAY_POLL_INTERVAL_S` (default 0.5s).
- **One active relay, enforced by Postgres.** The worker takes a
  session-level advisory lock (`pg_try_advisory_lock`) on the connection
  it drains with, and drains only while it holds that lock. A second
  instance (a rollout overlap, a stray replica) waits for the lock instead
  of interleaving, so one key's messages stay in `id` order. If the
  connection drops, the lock goes with it and draining stops. No Redis
  lock.
- **Failures**: Kafka unreachable → rows stay `PENDING`, no attempts
  counted, and the loop backs off. Message-specific failure → `attempts`
  increments, and after 5 the row is `DEAD`. An `outbox` row also goes to
  `alert.received.dlq`. A `verdict_outbox` row is logged and kept (no DLQ
  topic, ADR-0024). Attempt counting and the DLQ arrive on Day 16.
- **Pruning** runs in the same worker on a timer: `SENT` rows older than
  `OUTBOX_RETENTION_H` (default 24h) are deleted from both tables. `DEAD`
  rows are kept.
- **Deployment**: one more container in Compose (`outbox-relay`) and one
  Deployment on kind, `replicas: 1`. Because of the advisory lock, an
  overlapping pod during a rollout is harmless.
- **Scheduled batch work isn't the relay's job.** The Day 25 eval is a
  script, run on demand with `uv run` and nightly by a Kubernetes
  `CronJob` on the kind cluster.

## Alternatives considered

- **Celery task scheduled by `celery-beat`** (ADR-0008, before this
  change). See Context: a lock to fake a loop, a sync/async mismatch, and
  three containers for one job.
- **Relay loop inside `ingestion`** (a background task in its process).
  Fewest containers. Rejected because the relay also drains
  `review-console`'s table, and because with `ingestion` at 2 replicas
  (Day 19) it would compete for the lock and share request workers.
- **Postgres `LISTEN/NOTIFY` to wake the relay** instead of polling.
  Lower latency, but notifications are lost while the relay is
  disconnected, so polling is still needed underneath. Reasonable to add
  later if 0.5s matters.
- **Change data capture (Debezium).** Robust, but a heavy component (Kafka
  Connect) for two small tables.
- **Publish directly from the services.** Loses alerts on a Kafka outage
  and caused the Day 11 verdict mismatch (ADR-0024).

## Consequences

- ✅ No accepted alert or recorded verdict is lost, and failures are
  visible (`DEAD` rows, DLQ).
- ✅ One container instead of `celery-worker` + `celery-beat`, no Redis
  broker DB, no Redis lock. Same async clients as every service.
- ✅ Lower added latency than a 1s beat tick: up to one poll interval
  when idle, none while there's a backlog.
- ❌ At-least-once delivery: a crash between publish and `SENT`
  re-publishes, so every consumer must dedupe (Day 16).
- ❌ A single active relay caps relay throughput at one process. That's
  fine at this build's rates. Partitioning the relay by key would be the
  next step.
- ❌ No general background-job framework. A future scheduled job is a
  `CronJob` or a loop in its own worker, chosen when it's needed.
