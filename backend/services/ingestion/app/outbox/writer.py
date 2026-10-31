"""Writes accepted alerts to the Postgres `outbox` (Day 15, ADR-0030).

`ingestion` never publishes to Kafka: `POST /apps/{app_id}/events` inserts
one `outbox` row (topic `alert.received`, `kafka_key` = `{app_id}:{alert_key}`,
the protobuf `RunAgentRequest`, and the current trace context as headers)
and returns `202` once it's committed. The `outbox-relay` worker publishes
`PENDING` rows to Kafka in `id` order (docs/ARCHITECTURE.md §10). So an
accepted alert survives a Kafka outage, and a `503` now means only that
Postgres didn't take the row: nothing was written, and a retry is safe.
"""

import json
import logging
from dataclasses import dataclass
from typing import Protocol

import asyncpg
from opentelemetry.propagate import inject

logger = logging.getLogger(__name__)

ALERT_RECEIVED = "alert.received"
# Fail fast: intake must not hang on a stuck database.
WRITE_TIMEOUT_S = 5.0


class OutboxError(RuntimeError):
    """The row wasn't written; the caller should retry later."""


@dataclass(frozen=True)
class OutboxEntry:
    app_id: str
    alert_id: str
    topic: str
    kafka_key: str
    payload: bytes
    headers: dict[str, str]


class Outbox(Protocol):
    async def write(self, entry: OutboxEntry) -> None: ...


def trace_headers() -> dict[str, str]:
    """W3C trace context of the current span (empty when there's none)."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


class PostgresOutbox:
    """The pool is created on `start()`; a failure there is retried on the
    next write, so the service starts even while Postgres is down."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, timeout=WRITE_TIMEOUT_S)
        return self._pool

    async def start(self) -> None:
        try:
            await self._get_pool()
        except (OSError, asyncpg.PostgresError, TimeoutError) as e:
            logger.warning("Postgres unavailable at startup (%s); will retry on the first alert", e)

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def write(self, entry: OutboxEntry) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(
                "INSERT INTO outbox (app_id, alert_id, topic, kafka_key, payload, headers) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                entry.app_id, entry.alert_id, entry.topic, entry.kafka_key, entry.payload,
                json.dumps(entry.headers, separators=(",", ":")), timeout=WRITE_TIMEOUT_S,
            )
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError) as e:
            raise OutboxError(f"outbox write failed: {type(e).__name__}") from e

