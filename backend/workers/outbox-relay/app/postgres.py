"""Postgres side of the relay: one connection per session (ADR-0030).

The advisory lock is a *session* lock (`pg_try_advisory_lock`), so it's
held by this connection and released when the connection closes or
drops. Every asyncpg/connection error becomes `DatabaseUnavailable`, which
ends the session in `Relay.run`.
"""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import asyncpg

from app.relay import TABLES, DatabaseUnavailable, OutboxRow, OutboxTable

CONNECT_TIMEOUT_S = 5.0
# Table names go into SQL text, so only the known ones are accepted.
_KNOWN_TABLES = {t.name for t in TABLES}
# InternalClientError: what asyncpg can raise when the server kills the
# connection mid-operation (seen with pg_terminate_backend).
_ERRORS = (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError, asyncpg.InternalClientError)


def _table(table: OutboxTable) -> str:
    if table.name not in _KNOWN_TABLES:
        raise ValueError(f"unknown outbox table {table.name!r}")
    return table.name


class _Claim:
    def __init__(self, conn: asyncpg.Connection, table: str, rows: list[OutboxRow]):
        self._conn = conn
        self._table = table
        self.rows = rows

    async def mark_sent(self, ids: Sequence[int]) -> None:
        await self._conn.execute(
            f"UPDATE {self._table} SET status = 'SENT', sent_at = now() WHERE id = ANY($1::bigint[])", list(ids)
        )

    async def mark_failed(self, row_id: int, error: str, attempts: int) -> None:
        await self._conn.execute(
            f"UPDATE {self._table} SET attempts = $2, last_error = $3 WHERE id = $1", row_id, attempts, error
        )

    async def mark_dead(self, row_id: int, error: str, attempts: int) -> None:
        await self._conn.execute(
            f"UPDATE {self._table} SET status = 'DEAD', attempts = $2, last_error = $3 WHERE id = $1",
            row_id, attempts, error,
        )


class PostgresSession:
    def __init__(self, conn: asyncpg.Connection, lock_key: int):
        self._conn = conn
        self._lock_key = lock_key

    async def try_lock(self) -> bool:
        try:
            return bool(await self._conn.fetchval("SELECT pg_try_advisory_lock($1)", self._lock_key))
        except _ERRORS as e:
            raise DatabaseUnavailable(f"{type(e).__name__}: {e}") from e

    @asynccontextmanager
    async def claim(self, table: OutboxTable, limit: int) -> AsyncIterator[_Claim]:
        name = _table(table)
        try:
            async with self._conn.transaction():
                records = await self._conn.fetch(
                    f"SELECT id, topic, kafka_key, payload, headers, created_at, attempts FROM {name} "
                    "WHERE status = 'PENDING' ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED",
                    limit,
                )
                yield _Claim(self._conn, name, [OutboxRow(**dict(r)) for r in records])
        except _ERRORS as e:
            raise DatabaseUnavailable(f"{type(e).__name__}: {e}") from e

    async def prune(self, table: OutboxTable, retention_h: float) -> int:
        try:
            status = await self._conn.execute(
                f"DELETE FROM {_table(table)} WHERE status = 'SENT' "
                "AND sent_at < now() - make_interval(secs => $1)",
                retention_h * 3600,
            )
        except _ERRORS as e:
            raise DatabaseUnavailable(f"{type(e).__name__}: {e}") from e
        return int(status.split()[-1])

    async def close(self) -> None:
        try:
            await self._conn.close(timeout=CONNECT_TIMEOUT_S)
        except _ERRORS:
            self._conn.terminate()


class PostgresDatabase:
    def __init__(self, dsn: str, lock_key: int):
        self._dsn = dsn
        self._lock_key = lock_key

    async def connect(self) -> PostgresSession:
        try:
            conn = await asyncpg.connect(self._dsn, timeout=CONNECT_TIMEOUT_S)
            await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        except _ERRORS as e:
            raise DatabaseUnavailable(f"{type(e).__name__}: {e}") from e
        return PostgresSession(conn, self._lock_key)
