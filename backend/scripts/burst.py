# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "aiokafka>=0.12", "protobuf>=5.28", "psycopg[binary]>=3.2"]
# ///
"""Send a burst of alerts through `ingestion`, then check each was decided
exactly once (docs/plan.md Day 16; Day 21's simulator replaces it).

    uv run backend/scripts/burst.py send --count 20 --out /tmp/burst.json
    #   ... stop Kafka mid-burst, start it again ...
    uv run backend/scripts/burst.py check /tmp/burst.json

`send` posts `--count` alerts for `--app-id` over `--keys` keys, `--interval`
apart, and writes the accepted `alert_id`s to `--out`: `it-ops-triage` disk
alerts on hosts `burst-01`.., `cost-anomaly-triage` spend spikes on services
`burst-svc-01`.. (neither touches real history). Every post must get `202`:
since Day 15 intake doesn't depend on Kafka, and since Day 19 not on a
rollout either. A request that fails without a response (connection
refused or dropped) counts as a failure too. Each alert runs the real LLM.

On the kind cluster (Day 19), send through ingestion's NodePort, which
keeps working while pods are replaced, and check through the port-forwards:

    uv run backend/scripts/burst.py --ingestion http://localhost:18101 send --app-id it-ops-triage --out it.json
    uv run backend/scripts/burst.py --bootstrap localhost:39092 \
        --postgres postgresql://platform:platform@localhost:15432/platform check it.json

`check` waits until every alert's `outbox` row is `SENT` and every alert
has an `alert.decided`, then counts decisions per `alert_id` over the whole
topic, and reads each decision's reasons for platform failures: a tool call
that failed for an infrastructure reason ("tool 'X' failed (code)") or a
run that was "not evaluated". Exits 0 only if each alert was decided
exactly once and none of its decisions has such a failure.
"""

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import psycopg
from aiokafka import AIOKafkaConsumer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from proto_gen import agent_pb2  # noqa: E402


def event(app_id: str, run: str, i: int, keys: int) -> tuple[str, dict]:
    n = i % keys + 1
    if app_id == "cost-anomaly-triage":
        service = f"burst-svc-{n:02d}"
        return service, {
            "source": "burst",
            "severity": "warning",
            "message": f"Spend spike on {service} in us-east-1 (burst {run} #{i})",
            "payload": {
                "account": "burst-account", "service": service, "region": "us-east-1",
                "window_start": "2026-10-05T00:00:00Z", "window_end": "2026-10-06T00:00:00Z",
                "baseline_cost": 100.0, "observed_cost": 180.0,
            },
        }
    host = f"burst-{n:02d}"
    return host, {
        "source": "burst",
        "severity": "warning",
        "message": f"Disk usage 91% on {host} (burst {run} #{i})",
        "payload": {"alert_type": "disk_full", "host": host, "value": 91},
    }


def send(args: argparse.Namespace) -> int:
    run = uuid.uuid4().hex[:6]
    accepted, refused = [], []
    with httpx.Client(base_url=args.ingestion, timeout=10) as client:
        for i in range(args.count):
            key, body = event(args.app_id, run, i, args.keys)
            try:
                r = client.post(f"/apps/{args.app_id}/events", json=body)
            except httpx.TransportError as e:
                refused.append((i, "no response", f"{type(e).__name__}: {e}"))
                print(f"#{i:03d} {key} -> {type(e).__name__}", flush=True)
            else:
                if r.status_code == 202:
                    accepted.append(r.json())
                else:
                    refused.append((i, r.status_code, r.text[:200]))
                print(f"#{i:03d} {key} -> {r.status_code}", flush=True)
            time.sleep(args.interval)
    Path(args.out).write_text(json.dumps({"app_id": args.app_id, "alerts": accepted}, indent=1))
    print(f"accepted {len(accepted)}/{args.count}; ids in {args.out}")
    for i, code, text in refused:
        print(f"  refused #{i}: {code} {text}")
    return 0 if not refused else 1


TOOL_FAILURE = re.compile(r"^orchestrator: tool '[^']+' failed \(")
NOT_EVALUATED = "orchestrator: not evaluated:"


async def decided_counts(bootstrap: str, ids: set[str], failures: dict[str, list[str]]) -> Counter:
    consumer = AIOKafkaConsumer(
        "alert.decided", bootstrap_servers=bootstrap, auto_offset_reset="earliest",
        enable_auto_commit=False, group_id=None,
    )
    await consumer.start()
    counts: Counter = Counter()
    try:
        while True:
            batch = await consumer.getmany(timeout_ms=2000, max_records=5000)
            if not batch:
                return counts
            for messages in batch.values():
                for m in messages:
                    decided = agent_pb2.RunAgentResponse.FromString(m.value)
                    if decided.alert_id in ids:
                        counts[decided.alert_id] += 1
                        bad = [r for r in decided.reasons if TOOL_FAILURE.match(r) or r.startswith(NOT_EVALUATED)]
                        if bad:
                            failures[decided.alert_id] = bad
    finally:
        await consumer.stop()


def check(args: argparse.Namespace) -> int:
    alerts = json.loads(Path(args.file).read_text())["alerts"]
    ids = {a["alert_id"] for a in alerts}
    deadline = time.monotonic() + args.timeout
    while True:
        with psycopg.connect(args.postgres) as conn:
            statuses = Counter(
                status for (status,) in conn.execute("SELECT status FROM outbox WHERE alert_id = ANY(%s)", (list(ids),))
            )
        failures: dict[str, list[str]] = {}
        counts = asyncio.run(decided_counts(args.bootstrap, ids, failures))
        done = statuses.get("SENT", 0) == len(ids) and len(counts) == len(ids)
        print(f"outbox {dict(statuses)}; decided {len(counts)}/{len(ids)}", flush=True)
        if done or time.monotonic() > deadline:
            break
        time.sleep(5)
    twice = {a: n for a, n in counts.items() if n > 1}
    missing = ids - set(counts)
    print(f"exactly once: {sum(1 for n in counts.values() if n == 1)}/{len(ids)}; "
          f"duplicates: {len(twice)}; never decided: {len(missing)}")
    for a, n in twice.items():
        print(f"  decided {n}x: {a}")
    for a in missing:
        print(f"  never decided: {a}")
    print(f"decisions with a tool failure or not evaluated: {len(failures)}")
    for a, reasons in failures.items():
        print(f"  {a}: {reasons}")
    return 0 if not twice and not missing and not failures else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ingestion", default="http://localhost:8001")
    p.add_argument("--bootstrap", default="localhost:29092")
    p.add_argument("--postgres", default="postgresql://platform:platform@localhost:5432/platform")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--app-id", default="it-ops-triage")
    s.add_argument("--count", type=int, default=20)
    s.add_argument("--keys", type=int, default=4)
    s.add_argument("--interval", type=float, default=0.5)
    s.add_argument("--out", default="burst.json")
    c = sub.add_parser("check")
    c.add_argument("file")
    c.add_argument("--timeout", type=float, default=600)
    args = p.parse_args()
    return send(args) if args.cmd == "send" else check(args)


if __name__ == "__main__":
    sys.exit(main())
