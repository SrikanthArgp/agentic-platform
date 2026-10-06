# /// script
# requires-python = ">=3.12"
# dependencies = ["aiokafka>=0.12", "protobuf>=5.28"]
# ///
"""Hand-publish an `alert.received` event and wait for its `alert.decided`.

Publishes straight to Kafka, bypassing `ingestion` (no schema check; the
app must be registered for `orchestrator` to run it). Written for Day 3's
definition of done; still handy to see one decision without `review-console`.

    uv run backend/scripts/publish_alert.py                       # disk_full on web-01
    uv run backend/scripts/publish_alert.py --alert-type healthcheck_flap --host lb-02
    uv run backend/scripts/publish_alert.py --payload '{"alert_type": "service_down", "service": "checkout"}'
    uv run backend/scripts/publish_alert.py --no-wait

`--payload` replaces the default payload. Prints the decision as JSON; exits
1 if none arrives within `--timeout` seconds.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from google.protobuf.json_format import MessageToDict

# The committed proto stubs (backend/shared/proto_gen), without installing ap-shared.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from proto_gen import agent_pb2  # noqa: E402

ALERT_RECEIVED = "alert.received"
ALERT_DECIDED = "alert.decided"


def build_request(args: argparse.Namespace) -> agent_pb2.RunAgentRequest:
    payload = (
        json.loads(args.payload)
        if args.payload
        else {"alert_type": args.alert_type, "host": args.host, "metric": "disk_used_percent", "value": 91.0}
    )
    request = agent_pb2.RunAgentRequest(
        app_id=args.app_id,
        alert_id=str(uuid.uuid4()),
        alert_key=f"{payload.get('alert_type', 'unknown')}:{payload.get('host') or payload.get('service', 'unknown')}",
        source="publish_alert.py",
        severity=args.severity,
        message=args.message or f"{payload.get('alert_type')} on {payload.get('host') or payload.get('service')}",
        timestamp_unix_ms=int(time.time() * 1000),
    )
    request.payload.update(payload)
    return request


async def wait_for_decision(consumer: AIOKafkaConsumer, alert_id: str, timeout_s: float) -> dict | None:
    async def scan() -> dict:
        async for msg in consumer:
            response = agent_pb2.RunAgentResponse.FromString(msg.value)
            if response.alert_id == alert_id:
                return MessageToDict(response, preserving_proto_field_name=True)
        raise RuntimeError("consumer stopped")

    try:
        return await asyncio.wait_for(scan(), timeout_s)
    except TimeoutError:
        return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bootstrap", default="localhost:29092")
    parser.add_argument("--app-id", default="it-ops-triage")
    parser.add_argument("--alert-type", default="disk_full")
    parser.add_argument("--host", default="web-01")
    parser.add_argument("--severity", default="warning")
    parser.add_argument("--message", default="")
    parser.add_argument("--payload", help="full payload as a JSON object (replaces the default)")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()

    request = build_request(args)
    key = f"{request.app_id}:{request.alert_key}".encode()

    # Earliest + filter by alert_id: no race with orchestrator answering
    # before this consumer is assigned.
    consumer = None
    if not args.no_wait:
        consumer = AIOKafkaConsumer(ALERT_DECIDED, bootstrap_servers=args.bootstrap, auto_offset_reset="earliest")
        await consumer.start()
    producer = AIOKafkaProducer(bootstrap_servers=args.bootstrap)
    await producer.start()
    try:
        await producer.send_and_wait(ALERT_RECEIVED, request.SerializeToString(), key=key)
    finally:
        await producer.stop()
    print(json.dumps({"published": MessageToDict(request, preserving_proto_field_name=True)}, indent=2))

    if consumer is None:
        return 0
    try:
        decision = await wait_for_decision(consumer, request.alert_id, args.timeout)
    finally:
        await consumer.stop()
    if decision is None:
        print(f"no alert.decided for {request.alert_id} within {args.timeout:.0f}s", file=sys.stderr)
        return 1
    print(json.dumps({"decided": decision}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
