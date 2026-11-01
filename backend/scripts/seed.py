# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]>=3.2", "redis>=5.1", "httpx>=0.27"]
# ///
"""Seed synthetic history: decisions for `memory-store` (docs/plan.md Day 6)
and resolved past cases for `review-console` (Day 17).

    uv run backend/scripts/seed.py                    # it-ops-triage
    uv run backend/scripts/seed.py --app-id cost-anomaly-triage
    uv run backend/scripts/seed.py --app-id security-alert-triage
    uv run backend/scripts/seed.py --app-id it-ops-triage --reset   # remove seed rows only

Writes decision events straight into Postgres `memory_events` (the source
of truth, ADR-0017) at fixed offsets before *now*, and deletes the seeded
keys' Redis cache so the next `GetContext` rebuilds them from Postgres.
Then prints the counts `GetContext` should return for each key.

`SEED_CASES` adds resolved cases straight into Postgres `cases`, at fixed
dates, each with its decision and verdict in `memory_events` too, as the
real flow would have recorded them. They're what `similar-past-case-lookup`
returns, which is how an agent sees history older than memory's 7 days,
e.g. `cost-anomaly-triage`'s monthly batch job on `checkout-svc:us-east-1`
(its billing fixture has this month's run on 2026-10-01), and
`security-alert-triage`'s weekly vulnerability-scanner sweep on
`nids-portscan-sweep:vulnscan-01` (three analyst-confirmed noise cases) plus
one confirmed incident on `siem-impossible-travel:vpn-gw-01`.

Idempotent: every run first deletes this app's earlier seed rows (ref_id
or alert_id `seed:...`), so re-running just moves the decision history to
the current time; seeded cases keep their fixed dates. Real decisions and
cases for the same keys are left alone, and add to the counts.
The app must be registered (its `memory_namespace` comes from `registry`).
"""

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import redis
from psycopg.types.json import Jsonb

HOUR = 3600.0
WINDOWS_H = {"1h": 1, "24h": 24, "7d": 24 * 7}

# alert_key -> [(decision, hours_ago), ...]. Offsets stay clear of window
# edges, so the expected counts hold for a while after seeding.
SCENARIOS: dict[str, dict[str, list[tuple[str, float]]]] = {
    "it-ops-triage": {
        # A flapping health check: lots of recent noise, always suppressed.
        "healthcheck_flap:lb-02": (
            [("SUPPRESS", h) for h in (0.1, 0.25, 0.4, 0.6, 0.8)]
            + [("SUPPRESS", h) for h in (2, 4, 6, 9, 13, 18, 22)]
            + [("SUPPRESS", h) for h in (30, 50, 75, 100, 140)]
        ),
        # A disk that fills every few days and gets auto-resolved.
        "disk_full:web-01": [("AUTO_RESOLVE", h) for h in (5, 29, 77, 125)] + [("ESCALATE", 150)],
        # A real outage yesterday, escalated twice.
        "service_down:app-03": [("ESCALATE", 20), ("ESCALATE", 21.5)],
        # Recurring replication lag, mixed handling.
        "db_replication_lag:db-02": [("ESCALATE", 0.5), ("ESCALATE", 12), ("SUPPRESS", 36), ("ESCALATE", 96)],
        # Older than the 7-day window: counts are 0, but it's not novel.
        "cert_expiring:api-gw": [("ESCALATE", 24 * 9)],
    },
    "cost-anomaly-triage": {},  # its history is the past cases below
    "security-alert-triage": {},  # likewise
}


def _monthly_batch_case(month: int, amount: float, total: float) -> dict:
    run = datetime(2026, month, 1, 6, 0, tzinfo=UTC)
    resource = f"emr-recon-2026{month:02d}"
    return {
        "alert_key": "checkout-svc:us-east-1",
        "agent_id": "cost-triage-agent",
        "decided_at": run,
        "resolved_at": run + timedelta(hours=3),
        "confidence": 0.55,
        "reasons": [
            f"${amount:,.2f} of ${total:,.2f} on {run:%Y-%m-%d} is EMR cluster {resource}, tagged "
            "owner: finance-data, job: monthly-reconciliation, schedule: monthly",
            "no confirmed-noise past cases on checkout-svc:us-east-1 confirm the schedule yet; escalating so a human "
            "can confirm it once",
        ],
        "alert": {
            "source": "cost-anomaly-detector",
            "severity": "warning",
            "message": f"checkout-svc spend in us-east-1 is {total / 421.15:.1f}x baseline",
            "fired_at": (run + timedelta(hours=1)).isoformat(),
            "payload": {
                "account": "123456789012", "service": "checkout-svc", "region": "us-east-1",
                "window_start": f"{run:%Y-%m-%d}T00:00:00Z",
                "window_end": f"{run + timedelta(days=1):%Y-%m-%d}T00:00:00Z",
                "baseline_cost": 421.15, "observed_cost": total,
            },
        },
        "verdict": "CONFIRMED_NOISE",
        "resolution_notes": (
            f"Expected: finance-data's monthly reconciliation EMR batch ({resource}) runs on the 1st of every "
            "month and is torn down the same day. No action needed."
        ),
    }


def _scanner_sweep_case(day: int) -> dict:
    """secops' weekly authenticated scan trips the port-sweep rule on the
    scanner itself; analysts confirmed it as noise each time."""
    run = datetime(2026, 10, day, 2, 0, tzinfo=UTC)
    return {
        "alert_key": "nids-portscan-sweep:vulnscan-01",
        "agent_id": "security-triage-agent",
        "decided_at": run,
        "resolved_at": run + timedelta(hours=7),
        "confidence": 0.5,
        "reasons": [
            "src_ip 10.20.0.15 is known-benign (asset-inventory: vulnscan-01, the internal vulnerability scanner)",
            "no confirmed-noise past cases on nids-portscan-sweep:vulnscan-01 yet; escalating so an analyst confirms it",
        ],
        "alert": {
            "source": "nids",
            "severity": "low",
            "message": "Port sweep from 10.20.0.15 across 10.40.0.0/24",
            "fired_at": run.isoformat(),
            "payload": {
                "rule_id": "nids-portscan-sweep", "host": "vulnscan-01", "src_ip": "10.20.0.15",
                "indicators": [{"type": "ip", "value": "10.20.0.15"}],
            },
        },
        "verdict": "CONFIRMED_NOISE",
        "resolution_notes": "Weekly authenticated scan by secops' vulnerability scanner (vulnscan-01). No action needed.",
    }


def _impossible_travel_incident() -> dict:
    run = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
    return {
        "alert_key": "siem-impossible-travel:vpn-gw-01",
        "agent_id": "security-triage-agent",
        "decided_at": run,
        "resolved_at": run + timedelta(hours=2),
        "confidence": 0.9,
        "reasons": ["VPN login for a.patel from 198.51.100.23 (known-bad: brute-force source) 40 minutes after one from Pune"],
        "alert": {
            "source": "siem",
            "severity": "medium",
            "message": "Impossible travel for a.patel on vpn-gw-01",
            "fired_at": run.isoformat(),
            "payload": {
                "rule_id": "siem-impossible-travel", "host": "vpn-gw-01", "src_ip": "198.51.100.23",
                "user": "a.patel", "indicators": [{"type": "ip", "value": "198.51.100.23"}],
            },
        },
        "verdict": "CONFIRMED_INCIDENT",
        "resolution_notes": "Credential stuffing succeeded; account disabled and password reset, MFA enforced.",
    }


# app_id -> resolved past cases (seeded with verdict_by "seed"). Fixed dates.
SEED_CASES: dict[str, list[dict]] = {
    "cost-anomaly-triage": [
        _monthly_batch_case(7, 1121.40, 1542.55),
        _monthly_batch_case(8, 1139.75, 1560.90),
        _monthly_batch_case(9, 1132.10, 1553.25),
    ],
    "security-alert-triage": [
        _scanner_sweep_case(11),
        _scanner_sweep_case(18),
        _scanner_sweep_case(25),
        _impossible_travel_incident(),
    ],
}


def expected(events: list[tuple[str, float]]) -> dict[str, tuple[int, int, int]]:
    """(alerts, escalations, suppressions) per window."""
    out = {}
    for window, hours in WINDOWS_H.items():
        inside = [d for d, h in events if h < hours]
        out[window] = (len(inside), inside.count("ESCALATE"), inside.count("SUPPRESS"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-id", default="it-ops-triage")
    parser.add_argument("--reset", action="store_true", help="only remove this app's seed rows and cache")
    parser.add_argument("--postgres-dsn", default="postgresql://platform:platform@localhost:5432/platform")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--registry-url", default="http://localhost:8005")
    args = parser.parse_args()

    scenario = SCENARIOS.get(args.app_id)
    seed_cases = SEED_CASES.get(args.app_id, [])
    if scenario is None:
        print(f"no seed scenario for app '{args.app_id}'; have {sorted(SCENARIOS)}", file=sys.stderr)
        return 1
    response = httpx.get(f"{args.registry_url}/apps/{args.app_id}", timeout=5)
    if response.status_code != 200:
        print(f"app '{args.app_id}' is not registered in registry ({response.status_code}); "
              f"run register_app.py first", file=sys.stderr)
        return 1
    namespace = response.json()["memory_namespace"]

    now = time.time()
    with psycopg.connect(args.postgres_dsn) as conn:
        deleted = conn.execute(
            "DELETE FROM memory_events WHERE app_id = %s AND ref_id LIKE 'seed:%%'", (args.app_id,)
        ).rowcount
        deleted_cases = conn.execute(
            "DELETE FROM cases WHERE app_id = %s AND alert_id LIKE 'seed:%%'", (args.app_id,)
        ).rowcount
        inserted = 0
        case_ids: list[tuple[int, dict]] = []
        if not args.reset:
            for i, c in enumerate(seed_cases):
                case_id = conn.execute(
                    "INSERT INTO cases (app_id, alert_id, alert_key, agent_id, decision, confidence, reasons, "
                    "tool_calls, alert, status, verdict, verdict_by, resolution_notes, created_at, resolved_at) "
                    "VALUES (%s, %s, %s, %s, 'ESCALATE', %s, %s, '[]'::jsonb, %s, 'RESOLVED', %s, 'seed', %s, %s, %s) "
                    "RETURNING id",
                    (args.app_id, f"seed:{c['alert_key']}:{i}", c["alert_key"], c["agent_id"], c["confidence"],
                     Jsonb(c["reasons"]), Jsonb(c["alert"]), c["verdict"], c["resolution_notes"],
                     c["decided_at"], c["resolved_at"]),
                ).fetchone()[0]
                case_ids.append((case_id, c))
                # What the real flow records: the decision, then the verdict (ADR-0016/0017).
                conn.execute(
                    "INSERT INTO memory_events (app_id, alert_key, kind, ref_id, occurred_at) VALUES "
                    "(%s, %s, 'decision:ESCALATE', %s, %s), (%s, %s, %s, %s, %s)",
                    (args.app_id, c["alert_key"], f"seed:{c['alert_key']}:{i}", c["decided_at"],
                     args.app_id, c["alert_key"], f"verdict:{c['verdict']}", f"seed:case:{c['alert_key']}:{i}",
                     c["resolved_at"]),
                )
                inserted += 2
            for alert_key, events in scenario.items():
                for i, (decision, hours_ago) in enumerate(events):
                    conn.execute(
                        "INSERT INTO memory_events (app_id, alert_key, kind, ref_id, occurred_at) "
                        "VALUES (%s, %s, %s, %s, to_timestamp(%s))",
                        (args.app_id, alert_key, f"decision:{decision}", f"seed:{alert_key}:{i}", now - hours_ago * HOUR),
                    )
                    inserted += 1

    keys = set(scenario) | {c["alert_key"] for c in seed_cases}
    cache = redis.Redis.from_url(args.redis_url)
    if keys:
        cache.delete(*(f"mem:{namespace}:{k}:{part}" for k in keys for part in ("events", "facts")))

    print(f"{args.app_id} (namespace {namespace}): deleted {deleted} seed memory rows and {deleted_cases} seed "
          f"cases, inserted {inserted} memory rows and {len(case_ids)} cases, cleared {len(keys)} cached keys")
    if args.reset:
        return 0
    for case_id, c in case_ids:
        print(f"  case {case_id}: {c['alert_key']} decided {c['decided_at']:%Y-%m-%d}, {c['verdict']}")
    if not scenario:
        return 0
    print("\nExpected GetContext (alerts/escalations/suppressions), plus any real decisions:")
    print(f"  {'alert_key':28} {'1h':>9} {'24h':>9} {'7d':>9}")
    for alert_key, events in scenario.items():
        e = expected(events)
        cells = " ".join(f"{'/'.join(map(str, e[w])):>9}" for w in WINDOWS_H)
        print(f"  {alert_key:28} {cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
