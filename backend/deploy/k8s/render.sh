#!/usr/bin/env bash
# Render the local kind manifests for one image tag to stdout:
#
#   backend/deploy/k8s/render.sh TAG | kubectl apply -f -
#
# Wraps overlays/local in a throwaway kustomization that
#   - sets every platform image to TAG (the git SHA k8s_up.sh built),
#   - generates ConfigMaps from the files Compose already uses, so there is
#     one copy of each: postgres-init (backend/local/postgres/init.sql) and
#     kafka-topics-script (backend/local/kafka/create-topics.sh),
#   - generates the llm-api-key Secret from ENV_FILE (default
#     backend/local/.env, gitignored; empty if missing, so orchestrator runs
#     but publishes ESCALATE "not evaluated").
# TAG goes on all seven platform images, so it must be one `kind load`ed for
# all seven, as k8s_up.sh does. After rollout_app.sh the three rolled
# services run a newer tag than the rest; rendering with that tag would
# point the other four at an image the node doesn't have (they'd sit in
# ImagePullBackOff while their old pods keep serving; seen on Day 19). Use
# k8s_up.sh to apply manifest changes.
# Generated names get a content hash, so a changed key or init.sql rolls the
# pods that use it. The Secret exists only in the rendered output, and in a
# temp copy of ENV_FILE that is removed on exit.
set -euo pipefail

TAG="${1:?usage: render.sh TAG}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(cd "$HERE/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$BACKEND/local/.env}"
IMAGES="ingestion orchestrator tool-gateway memory-store registry review-console outbox-relay"

# Inside deploy/k8s (gitignored .render.*): kustomize takes a directory
# resource only as a relative path, and it must resolve without symlinks.
tmp="$(mktemp -d "$HERE/.render.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
cp "$BACKEND/local/postgres/init.sql" "$tmp/init.sql"
cp "$BACKEND/local/kafka/create-topics.sh" "$tmp/create-topics.sh"
if [[ -f "$ENV_FILE" ]]; then
  # Keep only KEY=VALUE lines (no comments/blank lines) and drop one pair
  # of surrounding quotes, as Compose's env_file does: secretGenerator keeps
  # them, and a quoted key is a 401 from the LLM provider.
  { grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" || true; } \
    | sed -E "s/^([^=]+)=\"(.*)\"[[:space:]]*\$/\1=\2/; s/^([^=]+)='(.*)'[[:space:]]*\$/\1=\2/" \
    > "$tmp/llm.env"
else
  : > "$tmp/llm.env"
fi

{
  echo "apiVersion: kustomize.config.k8s.io/v1beta1"
  echo "kind: Kustomization"
  echo "namespace: agentic-platform"
  echo "resources:"
  echo "  - ../overlays/local"
  echo "images:"
  for i in $IMAGES; do echo "  - { name: agentic-platform/$i, newTag: \"$TAG\" }"; done
  echo "configMapGenerator:"
  echo "  - { name: postgres-init, files: [init.sql] }"
  echo "  - { name: kafka-topics-script, files: [create-topics.sh] }"
  echo "secretGenerator:"
  echo "  - { name: llm-api-key, envs: [llm.env] }"
} > "$tmp/kustomization.yaml"

kubectl kustomize "$tmp"
