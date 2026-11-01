#!/usr/bin/env bash
# Forward the kind cluster's services to the host on ports that never clash
# with Compose's (Compose port + 10000; Kafka 39092, its HOST listener):
#
#   ingestion 18001  orchestrator 18002 (gRPC 50062)  tool-gateway 18003
#   memory-store 18004 (gRPC 50063)  registry 18005  review-console 18006
#   outbox-relay 18007  postgres 15432  redis 16379  kafka 39092
#
# Runs in the foreground until Ctrl-C (then stops every forward). A
# port-forward to a Service is pinned to one pod and ends when that pod is
# replaced (a rollout, Day 19), so each forward restarts itself; expect a
# second or so of refused connections then. For load during a rollout use
# ingestion's NodePort, http://localhost:18101 (kind-config.yaml), which
# never breaks.
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-agentic-platform}"
KUBECTL=(kubectl --context "kind-$CLUSTER" -n agentic-platform)
FORWARDS=(
  "svc/ingestion 18001:8000"
  "svc/orchestrator 18002:8000 50062:50051"
  "svc/tool-gateway 18003:8000"
  "svc/memory-store 18004:8000 50063:50051"
  "svc/registry 18005:8000"
  "svc/review-console 18006:8000"
  "svc/outbox-relay 18007:8000"
  "svc/postgres 15432:5432"
  "svc/redis 16379:6379"
  "pod/kafka-0 39092:39092"
)

pids=()
# The whole process group: each restart loop and the kubectl it's running.
trap 'trap - EXIT INT TERM; kill 0 2>/dev/null || true' EXIT INT TERM
for f in "${FORWARDS[@]}"; do
  # shellcheck disable=SC2086
  ( while true; do "${KUBECTL[@]}" port-forward $f >/dev/null 2>&1 || true; sleep 1; done ) &
  pids+=($!)
done
echo "forwarding (Ctrl-C to stop): ${FORWARDS[*]}"
wait
