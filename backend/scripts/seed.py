# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]>=3.2", "redis>=5.1", "httpx>=0.27"]
# ///
"""Seed `memory-store` with synthetic decision history (docs/plan.md Day 6).

    uv run backend/scripts/seed.py                    # it-ops-triage
    uv run backend/scripts/seed.py --app-id it-ops-triage --reset   # remove seed rows only

Writes decision events straight into Postgres `memory_events` (the source
of truth, ADR-0017) at fixed offsets before *now*, and deletes the seeded
keys' Redis cache so the next `GetContext` rebuilds them from Postgres.
Then prints the counts `GetContext` should return for each key.

Idempotent: every run first deletes this app's earlier seed rows (ref_id
`seed:...`), so re-running just moves the history to the current time.
Real decisions for the same keys are left alone, and add to the counts.
The app must be registered (its `memory_namespace` comes from `registry`).
"""

import argparse
import sys
import time

import httpx
import psycopg
import redis

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
        inserted = 0
        if not args.reset:
            for alert_key, events in scenario.items():
                for i, (decision, hours_ago) in enumerate(events):
                    conn.execute(
                        "INSERT INTO memory_events (app_id, alert_key, kind, ref_id, occurred_at) "
                        "VALUES (%s, %s, %s, %s, to_timestamp(%s))",
                        (args.app_id, alert_key, f"decision:{decision}", f"seed:{alert_key}:{i}", now - hours_ago * HOUR),
                    )
                    inserted += 1

    cache = redis.Redis.from_url(args.redis_url)
    cache.delete(*(f"mem:{namespace}:{k}:{part}" for k in scenario for part in ("events", "facts")))

    print(f"{args.app_id} (namespace {namespace}): deleted {deleted} seed rows, inserted {inserted}, "
          f"cleared {len(scenario)} cached keys")
    if args.reset:
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
