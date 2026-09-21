#!/usr/bin/env bash
# Regenerate Python gRPC stubs from backend/proto/*.proto into
# backend/shared/proto_gen/. Run after editing any .proto file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="$ROOT/proto"
OUT_DIR="$ROOT/shared/proto_gen"

cd "$ROOT/shared"
uv run python -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    --pyi_out="$OUT_DIR" \
    "$PROTO_DIR"/*.proto

# grpc_tools emits absolute-style imports (e.g. `import agent_pb2`) which
# only resolve when proto_gen/ itself is on sys.path. Rewrite to
# package-relative imports so `from proto_gen import agent_pb2` works
# normally from consuming services.
for f in "$OUT_DIR"/*_pb2_grpc.py; do
    sed -i.bak -E 's/^import ([a-zA-Z_][a-zA-Z0-9_]*_pb2) as/from . import \1 as/' "$f"
    rm -f "$f.bak"
done

echo "Generated stubs in $OUT_DIR"
