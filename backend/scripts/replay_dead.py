# /// script
# requires-python = ">=3.12"
# dependencies = ["psycopg[binary]>=3.2"]
# ///
"""List DEAD outbox rows, and put one back to PENDING once its cause is fixed (Day 16).

    uv run backend/scripts/replay_dead.py                         # list DEAD rows in both tables
    uv run backend/scripts/replay_dead.py --table outbox --id 42   # send row 42 again
    uv run backend/scripts/replay_dead.py --table verdict_outbox --all

A row goes DEAD after the `outbox-relay` worker's 5th rejection of the
same message (ADR-0030). Replaying it sets `status = 'PENDING'` and
`attempts = 0` (`last_error` is kept until it's sent), and the relay picks
it up on its next poll. It is published in `id` order among PENDING rows,
so it goes out *after* any later rows for its key that were already sent:
replay only once the cause is fixed (e.g. a broker size limit raised), and
expect `orchestrator`/`memory-store` to see it late. A replayed alert row
that was also copied to `alert.received.dlq` is not removed from there.
"""

import argparse
import sys

import psycopg

TABLES = {"outbox": "alert_id", "verdict_outbox": "case_id"}


def list_dead(conn: psycopg.Connection) -> None:
    found = False
    for table, ref in TABLES.items():
        rows = conn.execute(
            f"SELECT id, app_id, {ref}, kafka_key, attempts, length(payload), created_at, last_error "
            f"FROM {table} WHERE status = 'DEAD' ORDER BY id"
        ).fetchall()
        for r in rows:
            found = True
            print(f"{table} id={r[0]} app={r[1]} {ref}={r[2]} key={r[3]} attempts={r[4]} "
                  f"bytes={r[5]} created={r[6]:%Y-%m-%d %H:%M:%S}\n    last_error: {r[7]}")
    if not found:
        print("no DEAD rows")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--postgres", default="postgresql://platform:platform@localhost:5432/platform")
    p.add_argument("--table", choices=sorted(TABLES))
    which = p.add_mutually_exclusive_group()
    which.add_argument("--id", type=int, help="replay this DEAD row")
    which.add_argument("--all", action="store_true", help="replay every DEAD row in --table")
    args = p.parse_args()

    with psycopg.connect(args.postgres) as conn:
        if args.id is None and not args.all:
            list_dead(conn)
            return 0
        if not args.table:
            p.error("--table is required with --id/--all")
        where, params = ("id = %s", (args.id,)) if args.id is not None else ("TRUE", ())
        cur = conn.execute(
            f"UPDATE {args.table} SET status = 'PENDING', attempts = 0 "
            f"WHERE status = 'DEAD' AND {where} RETURNING id", params
        )
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            print("no matching DEAD row" + (f" (id {args.id})" if args.id is not None else ""))
            return 1
        print(f"{args.table}: back to PENDING: {', '.join(map(str, ids))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
