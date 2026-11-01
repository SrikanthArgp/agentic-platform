# ADR-0033: Sticky (eager) rebalancing, with the current alert finished before a partition moves

- **Status**: Accepted
- **Date**: 2026-11-01
- **See**: `ARCHITECTURE.md` §4, §10; `plan.md` Day 19; ADR-0014, ADR-0002

## Context

ADR-0014 says `orchestrator` uses cooperative rebalancing, so a pod swap
doesn't pause every partition. `orchestrator` consumes with aiokafka
(0.14), which implements only the eager rebalance protocol: on every
rebalance each member gives up all its partitions, then gets an
assignment. Its assignors are range, round-robin and sticky; there is no
cooperative-sticky.

Eager rebalancing also has a correctness edge. An agent run takes seconds
(an LLM, several tool calls). If a member is mid-run when its partitions
are revoked, the partition's next owner starts from the last committed
offset and runs the same alert again while the first is still running.
The `seen:{app_id}:{alert_id}` marker (Day 16) is set only after a
decision is published, so it doesn't stop that: two decisions are
published.

## Decision

- **Sticky assignor** (`StickyPartitionAssignor`): every rebalance still
  revokes everything, but most partitions go back to the member that had
  them.
- **Finish before giving up**: `on_partitions_revoked` (awaited by aiokafka
  before it rejoins, so the group waits) waits until the alert in progress
  is published and its offset committed. From then until the new
  assignment arrives the member takes no new message. A message it fetched
  but didn't start is never committed, and its partition's next owner gets
  it.
- **Commit only after publish**: the committed offset is that message's
  `offset + 1`, after `alert.decided` is acknowledged (`acks=all`). A
  failed publish commits nothing.
- **Clean exit**: on SIGTERM the pipeline finishes the current alert,
  commits it, and leaves the group (`LeaveGroup`), so the others rebalance
  at once instead of after a session timeout.
- **Bounded runs**: a run longer than `RUN_TIMEOUT_S` (120s) is "not
  evaluated" (ESCALATE). That bounds how long a rebalance or a pod exit can
  wait, and the pod's `terminationGracePeriodSeconds` (150s) is set above it.

## Alternatives considered

- **Switch to confluent-kafka (librdkafka)**, which supports
  `cooperative-sticky`: only the partitions that move would pause. But it's
  a new client, with its own threading model under asyncio, for the one
  service that consumes in a group with long-running work, and the waiting
  this saves is seconds per pod swap, with alerts queuing in Kafka in the
  meantime, not lost.
- **Eager rebalance without waiting**: simplest, but a rebalance in the
  middle of a run publishes the alert twice (see Context).
- **Static membership** (`group_instance_id`): avoids a rebalance when a
  member restarts with the same id, but Deployment pods get new names, and a
  static member doesn't send `LeaveGroup`, so a stopped pod's partitions
  wait for its session timeout.

## Consequences

- ✅ A pod swap doesn't run an alert twice: the partition only moves once
  the alert in progress is committed.
- ✅ Shutdown and rebalance use the same mechanism ("finish the current
  alert, then let go"), tested together.
- ❌ Every rebalance pauses every partition, for as long as the slowest
  member's current alert. Alerts keep being accepted (`ingestion` →
  `outbox` → Kafka) and wait in Kafka; decisions arrive later.
- ❌ ADR-0014's "cooperative rebalancing" isn't what's built; this ADR
  replaces that one bullet. Revisit if pause time becomes a problem (more
  replicas, longer runs): that's when cooperative rebalancing pays for a
  client change.
