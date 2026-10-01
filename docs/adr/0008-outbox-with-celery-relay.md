# ADR-0008: Transactional outbox with a Celery relay, Redis as broker

- **Status**: Accepted
- **Date**: 2026-10-01
- **See**: `ARCHITECTURE.md` §4, §8, §10; ADR-0002

## Context

`ingestion` returns `202` before a decision exists. If it then fails to
publish to Kafka (broker down, rejected message), the alert is lost after
the source was told it was accepted. Someone also has to run scheduled
background jobs (the outbox relay, nightly evals).

## Decision

- `ingestion` writes each validated event to a Postgres `outbox` table
  (with its Kafka key and trace headers) and returns `202`; it never
  publishes directly.
- A **Celery task**, scheduled every ~1s by `celery-beat`, relays
  `PENDING` rows to Kafka in `id` order under a Redis lock (one run at a
  time, preserving per-key order). Kafka down → rows wait, no attempts
  counted. Message-specific failure → after 5 attempts, `DEAD` and
  published to `alert.received.dlq`.
- **Redis is the Celery broker** (own DB index); Celery also runs the
  Day 23 batch eval.

## Alternatives considered

- **Publish directly from `ingestion`** — loses alerts on a Kafka outage.
- **Relay as a loop inside `ingestion`** — fewer containers and arguably
  simpler; rejected so Celery has a clear job beyond evals, and so relay
  load doesn't share `ingestion`'s request workers. Reasonable to revisit.
- **Change data capture (Debezium)** — robust, but another heavy component.
- **RabbitMQ as Celery broker** — another service; Redis is already there.

## Consequences

- ✅ No accepted alert is lost; failures are visible in the DLQ.
- ❌ Up to ~1s added latency per alert (relay interval).
- ❌ At-least-once delivery → every consumer must dedupe.
- ❌ Two more containers (`celery-worker`, `celery-beat`) and a lock to
  reason about.
