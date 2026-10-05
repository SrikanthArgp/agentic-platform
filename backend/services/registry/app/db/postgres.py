"""The Postgres `Repository`: `tools` and `apps` (backend/local/postgres/init.sql).

Each upsert locks the row it compares against (`FOR UPDATE`), so two
concurrent registrations of the same app or tool can't both report
`changed: false` for different contents.
"""

import json
from typing import Any

import asyncpg

from app.core.models import AppRecord, Tool, UpsertResult

_TOOL_COLUMNS = (
    "tool_id, version, description, scope, app_id, input_schema, output_schema, "
    "read_only, enabled, created_at, updated_at"
)


async def _init_connection(conn: asyncpg.Connection) -> None:
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(type_name, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=5, init=_init_connection)


def _tool(row: asyncpg.Record) -> Tool:
    return Tool.model_validate(dict(row))


def _app(row: asyncpg.Record) -> AppRecord:
    return AppRecord.model_validate(dict(row))


class PostgresRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def get_tool(self, tool_id: str, version: str) -> Tool | None:
        row = await self._pool.fetchrow(
            f"SELECT {_TOOL_COLUMNS} FROM tools WHERE tool_id = $1 AND version = $2", tool_id, version
        )
        return _tool(row) if row else None

    async def list_tools(self) -> list[Tool]:
        rows = await self._pool.fetch(f"SELECT {_TOOL_COLUMNS} FROM tools ORDER BY tool_id, version")
        return [_tool(r) for r in rows]

    async def upsert_tool(self, tool: Tool) -> UpsertResult:
        d = tool.definition()
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {_TOOL_COLUMNS} FROM tools WHERE tool_id = $1 AND version = $2 FOR UPDATE",
                tool.tool_id, tool.version,
            )
            if row is None:
                # ON CONFLICT: a concurrent first registration of the same
                # tool version (no row to lock yet) loses quietly.
                status = await conn.execute(
                    "INSERT INTO tools (tool_id, version, description, scope, app_id, input_schema, "
                    "output_schema, read_only) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (tool_id, version) DO NOTHING",
                    tool.tool_id, tool.version, d["description"], d["scope"], d["app_id"],
                    d["input_schema"], d["output_schema"], d["read_only"],
                )
                inserted = status.endswith(" 1")
                return UpsertResult(created=inserted, changed=inserted)
            if _tool(row).definition() == d:
                return UpsertResult(created=False, changed=False)
            await conn.execute(
                "UPDATE tools SET description = $3, scope = $4, app_id = $5, input_schema = $6, "
                "output_schema = $7, read_only = $8, updated_at = now() WHERE tool_id = $1 AND version = $2",
                tool.tool_id, tool.version, d["description"], d["scope"], d["app_id"],
                d["input_schema"], d["output_schema"], d["read_only"],
            )
            return UpsertResult(created=False, changed=True)

    async def set_tool_enabled(self, tool_id: str, version: str, enabled: bool) -> Tool | None:
        row = await self._pool.fetchrow(
            "UPDATE tools SET enabled = $3, "
            "updated_at = CASE WHEN enabled = $3 THEN updated_at ELSE now() END "
            f"WHERE tool_id = $1 AND version = $2 RETURNING {_TOOL_COLUMNS}",
            tool_id, version, enabled,
        )
        return _tool(row) if row else None

    async def delete_tool(self, tool_id: str, version: str) -> bool:
        status = await self._pool.execute("DELETE FROM tools WHERE tool_id = $1 AND version = $2", tool_id, version)
        return status == "DELETE 1"

    async def get_app(self, app_id: str) -> AppRecord | None:
        row = await self._pool.fetchrow(
            "SELECT app_id, display_name, manifest, created_at, updated_at FROM apps WHERE app_id = $1", app_id
        )
        return _app(row) if row else None

    async def list_apps(self) -> list[AppRecord]:
        rows = await self._pool.fetch(
            "SELECT app_id, display_name, manifest, created_at, updated_at FROM apps ORDER BY app_id"
        )
        return [_app(r) for r in rows]

    async def upsert_app(self, app_id: str, display_name: str, manifest: dict[str, Any]) -> UpsertResult:
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT manifest FROM apps WHERE app_id = $1 FOR UPDATE", app_id)
            if row is None:
                status = await conn.execute(
                    "INSERT INTO apps (app_id, display_name, manifest) VALUES ($1, $2, $3) "
                    "ON CONFLICT (app_id) DO NOTHING",
                    app_id, display_name, manifest,
                )
                inserted = status.endswith(" 1")
                return UpsertResult(created=inserted, changed=inserted)
            if row["manifest"] == manifest:
                return UpsertResult(created=False, changed=False)
            await conn.execute(
                "UPDATE apps SET display_name = $2, manifest = $3, updated_at = now() WHERE app_id = $1",
                app_id, display_name, manifest,
            )
            return UpsertResult(created=False, changed=True)

    async def delete_app(self, app_id: str) -> bool:
        status = await self._pool.execute("DELETE FROM apps WHERE app_id = $1", app_id)
        return status == "DELETE 1"
