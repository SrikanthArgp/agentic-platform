# /// script
# requires-python = ">=3.12"
# dependencies = ["grpcio>=1.66", "protobuf>=5.28"]
# ///
"""gRPC test client for `memory-store`'s `GetContext` (docs/plan.md Day 6).

    uv run backend/scripts/get_context.py healthcheck_flap:lb-02
    uv run backend/scripts/get_context.py --app-id it-ops-triage disk_full:web-01 service_down:app-03
    uv run backend/scripts/get_context.py --repeat 500 healthcheck_flap:lb-02   # latency

Prints each response as JSON. With `--repeat N`, also calls the first key N
times on one channel and prints client-side latency (p50/p95/p99/max).
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import grpc
from google.protobuf.json_format import MessageToDict

# The committed proto stubs (backend/shared/proto_gen), without installing ap-shared.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from proto_gen import memory_store_pb2, memory_store_pb2_grpc  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("alert_keys", nargs="+")
    parser.add_argument("--app-id", default="it-ops-triage")
    parser.add_argument("--target", default="localhost:50053")
    parser.add_argument("--repeat", type=int, default=0)
    args = parser.parse_args()

    with grpc.insecure_channel(args.target) as channel:
        stub = memory_store_pb2_grpc.MemoryStoreStub(channel)

        def call(alert_key: str) -> memory_store_pb2.GetContextResponse:
            return stub.GetContext(memory_store_pb2.GetContextRequest(app_id=args.app_id, alert_key=alert_key), timeout=5)

        try:
            for alert_key in args.alert_keys:
                print(json.dumps(MessageToDict(call(alert_key), always_print_fields_with_no_presence=True), indent=2))
            if args.repeat:
                samples = []
                for _ in range(args.repeat):
                    start = time.perf_counter()
                    call(args.alert_keys[0])
                    samples.append((time.perf_counter() - start) * 1000)
                samples.sort()
                q = statistics.quantiles(samples, n=100)
                print(f"\nGetContext x{args.repeat} ({args.alert_keys[0]}): p50 {q[49]:.2f} ms, "
                      f"p95 {q[94]:.2f} ms, p99 {q[98]:.2f} ms, max {samples[-1]:.2f} ms")
        except grpc.RpcError as e:
            print(f"GetContext failed: {e.code().name}: {e.details()}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
