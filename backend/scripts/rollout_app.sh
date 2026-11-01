#!/usr/bin/env bash
# Roll ingestion, orchestrator and tool-gateway on the local kind cluster to
# a new image, then (optionally) register an app (docs/plan.md Day 19,
# ADR-0014, ADR-0006):
#
#   backend/scripts/rollout_app.sh                      # code only, no registration
#   backend/scripts/rollout_app.sh security-alert-triage
#   TAG=my-tag SKIP_BUILD=1 backend/scripts/rollout_app.sh   # roll an image already built and tagged
#
# Order, enforced here: build and tag (git SHA, -dirty-<time> with
# uncommitted backend/ changes, as k8s_up.sh) -> `kind load` -> `kubectl set
# image` on all three -> `kubectl rollout status` on all three -> only then
# `register_app.py`. If any rollout doesn't finish within ROLLOUT_TIMEOUT
# (default 300s; e.g. a new pod never passes /readyz), the script stops
# and nothing is registered: with maxUnavailable 0 the old pods are still
# serving, and `kubectl rollout undo deployment/NAME` puts that Deployment
# back. No event for a new app is accepted until every pod can handle it.
#
# Registration talks to registry and tool-gateway through port-forwards the
# script opens itself (host ports 18105/18103) after the rollout, so they
# reach the new pods; k8s_port_forward.sh needn't be running.
set -euo pipefail

APP_ID="${1:-}"
CLUSTER="${KIND_CLUSTER:-agentic-platform}"
NS=agentic-platform
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
BACKEND="$ROOT/backend"
KUBECTL=(kubectl --context "kind-$CLUSTER" -n "$NS")
ROLLED="ingestion orchestrator tool-gateway"

if [[ -n "$APP_ID" && ! -f "$BACKEND/apps/$APP_ID/manifest.yaml" ]]; then
  echo "no manifest at backend/apps/$APP_ID/manifest.yaml" >&2
  exit 1
fi
for tool in docker kind kubectl git uv; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
"${KUBECTL[@]}" get deployment ingestion >/dev/null || { echo "cluster $CLUSTER isn't up: run backend/scripts/k8s_up.sh" >&2; exit 1; }

if [[ -z "${TAG:-}" ]]; then
  TAG="$(git -C "$ROOT" rev-parse --short=12 HEAD)"
  if [[ -n "$(git -C "$ROOT" status --porcelain -- backend)" ]]; then
    TAG="$TAG-dirty-$(date +%Y%m%d%H%M%S)"
  fi
fi
echo "==> image tag $TAG"

images=()
for s in $ROLLED; do images+=("agentic-platform/$s:$TAG"); done
if [[ -z "${SKIP_BUILD:-}" ]]; then
  echo "==> building"
  for s in $ROLLED; do
    docker build -q -f "$BACKEND/services/$s/Dockerfile" -t "agentic-platform/$s:$TAG" "$BACKEND" >/dev/null
    echo "    agentic-platform/$s:$TAG"
  done
fi
echo "==> loading into kind"
kind load docker-image --name "$CLUSTER" "${images[@]}" >/dev/null

echo "==> rolling (maxUnavailable 0, maxSurge 1: old pods serve until new ones are ready)"
for s in $ROLLED; do
  "${KUBECTL[@]}" set image "deployment/$s" "$s=agentic-platform/$s:$TAG"
done
failed=()
for s in $ROLLED; do
  "${KUBECTL[@]}" rollout status "deployment/$s" --timeout="$ROLLOUT_TIMEOUT" || failed+=("$s")
done
if (( ${#failed[@]} )); then
  echo "rollout didn't finish for: ${failed[*]}. Old pods are still serving; nothing registered." >&2
  for s in "${failed[@]}"; do echo "  undo: kubectl --context kind-$CLUSTER -n $NS rollout undo deployment/$s" >&2; done
  exit 1
fi
"${KUBECTL[@]}" get pods -l 'app.kubernetes.io/name in (ingestion,orchestrator,tool-gateway)' -o wide

if [[ -z "$APP_ID" ]]; then
  echo "rolled to $TAG (code only, nothing registered)"
  exit 0
fi

echo "==> registering $APP_ID"
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT
"${KUBECTL[@]}" port-forward svc/registry 18105:8000 >/dev/null & pids+=($!)
"${KUBECTL[@]}" port-forward svc/tool-gateway 18103:8000 >/dev/null & pids+=($!)
for port in 18105 18103; do
  for _ in $(seq 50); do curl -sf "http://localhost:$port/healthz" >/dev/null && break; sleep 0.2; done
done
uv run "$BACKEND/scripts/register_app.py" "$APP_ID" \
  --registry-url http://localhost:18105 --tool-gateway-url http://localhost:18103/mcp
echo "rolled to $TAG and registered $APP_ID"
