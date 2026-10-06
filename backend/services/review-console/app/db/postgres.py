"""The Postgres `CaseStore`: the `cases` table (backend/local/postgres/init.sql)."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg

from app.core.cases import Case, NewCase, VerdictIn
from app.core.service import CaseQuery

_COLUMNS = (
    "id, app_id, alert_id, alert_key, agent_id, decision, confidence, reasons, tool_calls, alert, "
    "status, verdict, verdict_by, resolution_notes, created_at, resolved_at"
)


async def _init_connection(conn: asyncpg.Connection) -> None:
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10, init=_init_connection)


def _case(row: asyncpg.Record) -> Case:
    return Case.model_validate(dict(row))


class _LockedRow:
    def __init__(self, conn: asyncpg.Connection, case: Case | None):
        self._conn = conn
        self.case = case

    async def resolve(self, verdict: VerdictIn, at: datetime) -> Case:
        assert self.case is not None
        row = await self._conn.fetchrow(
            f"UPDATE cases SET status = 'RESOLVED', verdict = $2, verdict_by = $3, resolution_notes = $4, "
            f"resolved_at = $5 WHERE id = $1 RETURNING {_COLUMNS}",
            self.case.id, verdict.verdict.value, verdict.verdict_by, verdict.resolution_notes, at,
        )
        self.case = _case(row)
        return self.case


class PostgresCaseStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def add(self, case: NewCase) -> bool:
        status = await self._pool.execute(
            "INSERT INTO cases (app_id, alert_id, alert_key, agent_id, decision, confidence, reasons, tool_calls, "
            "alert) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (app_id, alert_id) DO NOTHING",
            case.app_id, case.alert_id, case.alert_key, case.agent_id, case.decision, case.confidence,
            case.reasons, [c.model_dump() for c in case.tool_calls],
            case.alert.model_dump(mode="json") if case.alert else None,
        )
        return status == "INSERT 0 1"

    async def get(self, case_id: int) -> Case | None:
        row = await self._pool.fetchrow(f"SELECT {_COLUMNS} FROM cases WHERE id = $1", case_id)
        return _case(row) if row else None

    async def list(self, query: CaseQuery) -> list[Case]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM cases WHERE app_id = $1 "
            "AND ($2::text IS NULL OR alert_key = $2) AND ($3::text IS NULL OR status = $3) "
            "ORDER BY created_at DESC, id DESC LIMIT $4 OFFSET $5",
            query.app_id, query.alert_key, query.status.value if query.status else None, query.limit, query.offset,
        )
        return [_case(r) for r in rows]

    @asynccontextmanager
    async def lock(self, case_id: int) -> AsyncIterator[_LockedRow]:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM cases WHERE id = $1 FOR UPDATE", case_id)
            yield _LockedRow(conn, _case(row) if row else None)
