"""`memory_events` in Postgres: the source of truth (docs/adr/0017).

Rows are keyed by `app_id` (the stable identity), never by
`memory_namespace`, which only names Redis keys (ADR-0018).
"""

from datetime import UTC, datetime
from typing import Protocol

import asyncpg

from app.core.events import CONFIRMED_INCIDENT, DECISION, Event, Facts


class EventStore(Protocol):
    async def insert(self, app_id: str, alert_key: str, event: Event) -> bool:
        """Store one event; False if (app_id, family, ref_id) was already stored."""
        ...

    async def load(self, app_id: str, alert_key: str, since_ms: int) -> tuple[list[Event], Facts]:
        """Events after `since_ms`, plus the all-time facts."""
        ...


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10)


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class PostgresEventStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def insert(self, app_id: str, alert_key: str, event: Event) -> bool:
        status = await self._pool.execute(
            "INSERT INTO memory_events (app_id, alert_key, kind, ref_id, occurred_at) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (app_id, family, ref_id) DO NOTHING",
            app_id, alert_key, event.kind, event.ref_id, _dt(event.at_ms),
        )
        return status == "INSERT 0 1"

    async def load(self, app_id: str, alert_key: str, since_ms: int) -> tuple[list[Event], Facts]:
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
            rows = await conn.fetch(
                "SELECT kind, ref_id, occurred_at FROM memory_events "
                "WHERE app_id = $1 AND alert_key = $2 AND occurred_at > $3 ORDER BY occurred_at",
                app_id, alert_key, _dt(since_ms),
            )
            facts = await conn.fetchrow(
                "SELECT "
                "EXISTS (SELECT 1 FROM memory_events WHERE app_id = $1 AND alert_key = $2 AND family = $3) AS seen, "
                "EXISTS (SELECT 1 FROM memory_events WHERE app_id = $1 AND alert_key = $2 AND kind = $4) AS confirmed",
                app_id, alert_key, DECISION, CONFIRMED_INCIDENT,
            )
        events = [Event(kind=r["kind"], ref_id=r["ref_id"], at_ms=_ms(r["occurred_at"])) for r in rows]
        return events, Facts(seen=facts["seen"], confirmed_incident=facts["confirmed"])
