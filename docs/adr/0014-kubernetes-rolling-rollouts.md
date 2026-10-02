# ADR-0014: Local Kubernetes from app #3 on; new apps ship by rolling update

- **Status**: Accepted
- **Date**: 2026-10-02
- **See**: `ARCHITECTURE.md` §11, §12; `plan.md` Days 18–20; ADR-0003, ADR-0006; rejects ADR-0013

## Context

Adding an app means rebuilding and restarting `ingestion`, `orchestrator`,
and `tool-gateway` (§12). Those containers are shared by every app
(ADR-0003), so under Compose every rollout briefly interrupts every other
app. Callers get errors from `ingestion`, and in-flight tool calls fail,
which pushes those alerts toward `ESCALATE`. ADR-0013 proposed hot-reloading
tools to avoid this. That only covers `tool-gateway`, only works when code
reaches the container without a new image, and adds a code-loading path to
a shared process (§13 T9).

The original plan kept Kubernetes out of scope (Compose only). Phase two
(`ENTERPRISE_READINESS.md` §6) already assumed Kubernetes as the runtime.

## Decision

- **Compose for apps #1 and #2** (`plan.md` Days 1–17), unchanged. It stays
  the local dev loop for the rest of the build.
- **A local Kubernetes cluster (kind) from Day 18**, built from the *same*
  images, with Kustomize manifests under `backend/deploy/k8s/`. It is
  ready before app #3. Still one Deployment per platform service, never per
  app (ADR-0003 unchanged).
- **App #3 and every app after it ship by rolling update**: new image tag
  for `ingestion`, `orchestrator`, `tool-gateway` → wait for all three
  rollouts to finish → `register_app.py`. Pods are still restarted, one at a
  time, with the old pods serving until new ones are ready. The ADR-0006
  rollout order (code first, manifest second) is kept: no event for the new
  app is accepted until every pod can handle it.
- **What a zero-interruption rolling update needs**, built on Day 19:
  - ≥ 2 replicas for the three services, `maxUnavailable: 0`, `maxSurge: 1`,
    and a PodDisruptionBudget.
  - A `/readyz` that means ready (e.g. `tool-gateway`'s tool scan finished),
    separate from the `/healthz` liveness check.
  - Graceful SIGTERM drain: stop taking work, finish in-flight requests,
    tool calls, and the current Kafka message, then exit.
  - `orchestrator` commits offsets only after processing and uses
    cooperative rebalancing.
  - The MCP client reconnects and retries a call when its connection drops.
    This is safe because every tool is read-only (ADR-0004).
- **ADR-0013 (tool hot-reload) is rejected**: rolling updates solve the
  same problem for all three services, without loading new code into a
  running process, and keep the image as the authoritative copy (§12).

## Alternatives considered

- **Stay on Compose throughout**: simplest. But every app rollout is a
  shared outage, and "add an app without disturbing the others" can't be
  shown.
- **Tool hot-reload (ADR-0013)**: covers only `tool-gateway`, needs a bind
  mount to be useful, and makes T9 worse.
- **Kubernetes from Day 1**: more work every day for no gain until there's
  a second app to protect. Compose is faster to iterate on.
- **Helm instead of Kustomize**: templating isn't needed for one local
  overlay. Kustomize ships with `kubectl`. Revisit for production
  packaging.
- **Managed cloud cluster now**: costs money and adds cloud credentials
  before any security controls exist (§13 phase-two gate). kind exercises
  the same rollout mechanics locally.

## Consequences

- ✅ Adding app #3 is shown to cause zero interruption for apps #1 and #2,
  measured under load on Day 20.
- ✅ The Day 19 drain and readiness work also helps Compose restarts and is
  what production Kubernetes needs anyway.
- ❌ Two deployment descriptions to keep in sync (Compose and Kustomize)
  for the rest of the build.
- ❌ Infra in kind (Kafka, Postgres, Redis as single-replica StatefulSets)
  is dev-grade. Managed services, HPA, and multi-node remain phase two.
- ❌ The build grows from 25 to 27 days.
