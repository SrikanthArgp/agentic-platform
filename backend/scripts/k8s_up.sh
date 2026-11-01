#!/usr/bin/env bash
# Bring the platform up on a local kind cluster (docs/plan.md Day 18,
# ADR-0014), from the same Dockerfiles Compose builds:
#
#   backend/scripts/k8s_up.sh            # create cluster if needed, build, load, apply, wait
#   KIND_CLUSTER=other backend/scripts/k8s_up.sh
#
# Images are tagged with the git SHA (plus -dirty-<time> when backend/ has
# uncommitted changes), never `latest`, so each run is a real tag change.
# Then: backend/scripts/k8s_port_forward.sh, and register_app.py / seed.py
# against the forwarded ports. Tear down with `kind delete cluster --name agentic-platform`.
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-agentic-platform}"
NS=agentic-platform
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
BACKEND="$ROOT/backend"
K8S="$BACKEND/deploy/k8s"
KUBECTL=(kubectl --context "kind-$CLUSTER")
SERVICES="registry ingestion orchestrator tool-gateway memory-store review-console"

for tool in docker kind kubectl git; do
  command -v "$tool" >/dev/null || { echo "missing: $tool (brew install kind; kubectl comes with Docker Desktop)" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker isn't running" >&2; exit 1; }

TAG="$(git -C "$ROOT" rev-parse --short=12 HEAD)"
if [[ -n "$(git -C "$ROOT" status --porcelain -- backend)" ]]; then
  TAG="$TAG-dirty-$(date +%Y%m%d%H%M%S)"
fi
echo "==> image tag $TAG"

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "==> creating kind cluster $CLUSTER"
  kind create cluster --name "$CLUSTER" --config "$K8S/kind-config.yaml" --wait 120s
fi

echo "==> building images"
images=()
for s in $SERVICES; do
  docker build -q -f "$BACKEND/services/$s/Dockerfile" -t "agentic-platform/$s:$TAG" "$BACKEND" >/dev/null
  echo "    agentic-platform/$s:$TAG"
  images+=("agentic-platform/$s:$TAG")
done
docker build -q -f "$BACKEND/workers/outbox-relay/Dockerfile" -t "agentic-platform/outbox-relay:$TAG" "$BACKEND" >/dev/null
echo "    agentic-platform/outbox-relay:$TAG"
images+=("agentic-platform/outbox-relay:$TAG")

echo "==> loading images into kind"
# Platform images only. The node pulls the public infra images (postgres,
# redis, kafka, otel) itself: `kind load` of a pulled multi-platform image
# fails on Docker Desktop's containerd image store ("content digest ...
# not found": the index lists platforms whose layers were never pulled).
kind load docker-image --name "$CLUSTER" "${images[@]}" >/dev/null 2>&1 \
  || kind load docker-image --name "$CLUSTER" "${images[@]}"

echo "==> applying manifests"
# A Job is immutable; delete the finished one so create-topics.sh reruns.
"${KUBECTL[@]}" -n "$NS" delete job kafka-topics --ignore-not-found >/dev/null 2>&1 || true
"$K8S/render.sh" "$TAG" | "${KUBECTL[@]}" apply -f -

echo "==> waiting for infra"
for sts in postgres redis kafka; do
  "${KUBECTL[@]}" -n "$NS" rollout status "statefulset/$sts" --timeout=300s
done
"${KUBECTL[@]}" -n "$NS" wait --for=condition=complete job/kafka-topics --timeout=300s

echo "==> waiting for platform"
for d in otel-collector $SERVICES outbox-relay; do
  "${KUBECTL[@]}" -n "$NS" rollout status "deployment/$d" --timeout=300s
done

"${KUBECTL[@]}" -n "$NS" get pods -o wide
cat <<MSG

All pods Ready (tag $TAG). Next:
  backend/scripts/k8s_port_forward.sh        # host ports 18001-18007, 15432, 16379, 39092
  uv run backend/scripts/register_app.py it-ops-triage --registry-url http://localhost:18005 --tool-gateway-url http://localhost:18003/mcp
MSG
