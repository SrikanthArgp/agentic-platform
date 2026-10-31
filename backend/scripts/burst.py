# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "aiokafka>=0.12", "protobuf>=5.28", "psycopg[binary]>=3.2"]
# ///
"""Send a burst of alerts through `ingestion`, then check each was decided
exactly once (docs/plan.md Day 16; Day 21's simulator replaces it).

    uv run backend/scripts/burst.py send --count 20 --out /tmp/burst.json
    #   ... stop Kafka mid-burst, start it again ...
    uv run backend/scripts/burst.py check /tmp/burst.json

`send` posts `--count` `it-ops-triage` disk alerts over `--keys` hosts
(`burst-01`.., so they don't touch real history), `--interval` apart, and
writes the accepted `alert_id`s to `--out`. Every post must get `202`: since
Day 15 intake doesn't depend on Kafka. Each alert runs the real LLM.

`check` waits until every alert's `outbox` row is `SENT` and every alert
has an `alert.decided`, then counts decisions per `alert_id` over the whole
topic. Exits 0 only if each was decided exactly once.
"""

import argparse
import asyncio
import json
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


def send(args: argparse.Namespace) -> int:
    run = uuid.uuid4().hex[:6]
    accepted, refused = [], []
    with httpx.Client(base_url=args.ingestion, timeout=10) as client:
        for i in range(args.count):
            host = f"burst-{i % args.keys + 1:02d}"
            body = {
                "source": "burst",
                "severity": "warning",
                "message": f"Disk usage 91% on {host} (burst {run} #{i})",
                "payload": {"alert_type": "disk_full", "host": host, "value": 91},
            }
            r = client.post(f"/apps/{args.app_id}/events", json=body)
            if r.status_code == 202:
                accepted.append(r.json())
            else:
                refused.append((i, r.status_code, r.text[:200]))
            print(f"#{i:03d} {host} -> {r.status_code}", flush=True)
            time.sleep(args.interval)
    Path(args.out).write_text(json.dumps({"app_id": args.app_id, "alerts": accepted}, indent=1))
    print(f"accepted {len(accepted)}/{args.count}; ids in {args.out}")
    for i, code, text in refused:
        print(f"  refused #{i}: {code} {text}")
    return 0 if not refused else 1


async def decided_counts(bootstrap: str, ids: set[str]) -> Counter:
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
                    alert_id = agent_pb2.RunAgentResponse.FromString(m.value).alert_id
                    if alert_id in ids:
                        counts[alert_id] += 1
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
        counts = asyncio.run(decided_counts(args.bootstrap, ids))
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
    return 0 if not twice and not missing else 1


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
