"""`registry`'s storage interface, and the in-memory version unit tests use.

Postgres (`postgres.py`) is the real one: `tools` and `apps` tables
(docs/ARCHITECTURE.md §8). Upserts report `created`/`changed` so
`register_app.py` can show that re-running it on an unchanged file did
nothing (ADR-0006).
"""

from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.models import AppRecord, Tool, UpsertResult


class Repository(Protocol):
    async def get_tool(self, tool_id: str, version: str) -> Tool | None: ...

    async def list_tools(self) -> list[Tool]: ...

    async def upsert_tool(self, tool: Tool) -> UpsertResult:
        """Insert or update a tool's definition; never touches `enabled`."""
        ...

    async def set_tool_enabled(self, tool_id: str, version: str, enabled: bool) -> Tool | None: ...

    async def delete_tool(self, tool_id: str, version: str) -> bool: ...

    async def get_app(self, app_id: str) -> AppRecord | None: ...

    async def list_apps(self) -> list[AppRecord]: ...

    async def upsert_app(self, app_id: str, display_name: str, manifest: dict[str, Any]) -> UpsertResult: ...

    async def delete_app(self, app_id: str) -> bool: ...


class InMemoryRepository:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], Tool] = {}
        self._apps: dict[str, AppRecord] = {}

    async def get_tool(self, tool_id: str, version: str) -> Tool | None:
        return self._tools.get((tool_id, version))

    async def list_tools(self) -> list[Tool]:
        return [self._tools[k] for k in sorted(self._tools)]

    async def upsert_tool(self, tool: Tool) -> UpsertResult:
        key = (tool.tool_id, tool.version)
        now = datetime.now(UTC)
        existing = self._tools.get(key)
        if existing is None:
            self._tools[key] = tool.model_copy(update={"enabled": True, "created_at": now, "updated_at": now})
            return UpsertResult(created=True, changed=True)
        if existing.definition() == tool.definition():
            return UpsertResult(created=False, changed=False)
        self._tools[key] = existing.model_copy(update={**tool.definition(), "updated_at": now})
        return UpsertResult(created=False, changed=True)

    async def set_tool_enabled(self, tool_id: str, version: str, enabled: bool) -> Tool | None:
        existing = self._tools.get((tool_id, version))
        if existing is None:
            return None
        if existing.enabled != enabled:
            existing = self._tools[(tool_id, version)] = existing.model_copy(
                update={"enabled": enabled, "updated_at": datetime.now(UTC)}
            )
        return existing

    async def delete_tool(self, tool_id: str, version: str) -> bool:
        return self._tools.pop((tool_id, version), None) is not None

    async def get_app(self, app_id: str) -> AppRecord | None:
        return self._apps.get(app_id)

    async def list_apps(self) -> list[AppRecord]:
        return [self._apps[k] for k in sorted(self._apps)]

    async def upsert_app(self, app_id: str, display_name: str, manifest: dict[str, Any]) -> UpsertResult:
        now = datetime.now(UTC)
        existing = self._apps.get(app_id)
        if existing is not None and existing.manifest == manifest:
            return UpsertResult(created=False, changed=False)
        self._apps[app_id] = AppRecord(
            app_id=app_id,
            display_name=display_name,
            manifest=manifest,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        return UpsertResult(created=existing is None, changed=True)

    async def delete_app(self, app_id: str) -> bool:
        return self._apps.pop(app_id, None) is not None
