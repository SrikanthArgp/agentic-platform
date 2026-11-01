# ADR-0032: Two-node local kind cluster

- **Status**: Accepted
- **Date**: 2026-11-01
- **See**: `ARCHITECTURE.md` §12; `plan.md` Day 19; ADR-0014

## Context

Day 18 built the local kind cluster with one node, and ADR-0014 left
multi-node to phase two. Day 19 gives `ingestion`, `orchestrator` and
`tool-gateway` two replicas, a PodDisruptionBudget, and a graceful drain.
On one node, both replicas of each share the node, so the PDB can't be
exercised by the disruption it exists for (`kubectl drain`), and spreading
replicas across nodes can't be shown at all. Docker has 16 GB; the
single-node cluster used about 2.5 GB.

## Decision

- **Two nodes**: one `control-plane` and one `worker`
  (`backend/deploy/k8s/kind-config.yaml`). kubeadm taints a control-plane
  NoSchedule once workers exist; the kind config sets `taints: []`, so
  both nodes run workloads. Measured cost: one more node container, about
  0.5 GB.
- **Replicated services spread** one pod per node
  (`topologySpreadConstraints` on `kubernetes.io/hostname`, `ScheduleAnyway`,
  `matchLabelKeys: [pod-template-hash]` so each ReplicaSet is spread on its
  own during a rollout). In `base/`, since it's what any multi-node cluster
  wants.
- **Everything else stays on the control-plane** (local overlay
  `nodeSelector`): the infra StatefulSets, whose data is on a node-local
  volume, and the single-replica services (`registry`, `memory-store`,
  `review-console`, `outbox-relay`, the OTel sink), which can't move without
  an outage. So the worker carries only replicated pods, and draining it
  interrupts nothing.
- **`ingestion` also on a NodePort** (host `127.0.0.1:18101`, kind
  `extraPortMappings`). `kubectl port-forward` is pinned to one pod and
  breaks when that pod is replaced, so load during a rollout goes through
  kube-proxy instead.

## Alternatives considered

- **Stay on one node** (ADR-0014 as written): enough for the rolling update
  itself, which replaces pods, not nodes. But the PDB and the spread would
  be configuration nothing tests.
- **One control-plane plus two workers, control-plane left tainted**: the
  "normal" kind layout, but a third node container (~0.5 GB more) for no
  extra coverage, and the infra still needs pinning to one node.
- **Pin nothing**: infra could land on the worker; draining it would then
  take Kafka or Postgres down, which isn't what the drain test is about.

## Consequences

- ✅ `kubectl drain` of the worker during a burst is a live check (Day 19):
  the PDB keeps one pod of each replicated service serving.
- ✅ Each replicated service survives losing a node, not just a pod.
- ❌ The local overlay knows node roles (the `nodeSelector` patches); a real
  cluster would use its own node pools instead.
- ❌ Draining the control-plane is still an outage (infra and singletons
  live there). HA for those remains phase two, as ADR-0014 says.
