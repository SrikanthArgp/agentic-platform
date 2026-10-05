"""`PostgresRepository` against the Compose Postgres (needs `tools`/`apps` from init.sql).

Uses its own `it-test-*` ids and deletes them afterwards, so it can run
against a stack that has real apps registered.
"""

import os
import uuid

import pytest

from app.core.models import Tool
from app.db.postgres import PostgresRepository, create_pool
from app.main import DEFAULT_POSTGRES_DSN

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def repo():
    pool = await create_pool(os.environ.get("POSTGRES_DSN", DEFAULT_POSTGRES_DSN))
    yield PostgresRepository(pool)
    await pool.close()


@pytest.fixture
def ids():
    suffix = uuid.uuid4().hex[:8]
    return f"it_test_{suffix}", f"it-test-{suffix}"


def make_tool(tool_id: str, app_id: str, description: str = "A tool.") -> Tool:
    return Tool(
        tool_id=tool_id, version="1.0.0", description=description, scope="app", app_id=app_id,
        input_schema={"type": "object", "properties": {"n": {"type": "number"}}}, output_schema=None, read_only=True,
    )


async def test_tool_upsert_is_idempotent_and_keeps_enabled(repo, ids):
    tool_id, app_id = ids
    try:
        assert (await repo.upsert_tool(make_tool(tool_id, app_id))).created
        assert not (await repo.upsert_tool(make_tool(tool_id, app_id))).changed
        await repo.set_tool_enabled(tool_id, "1.0.0", False)
        assert (await repo.upsert_tool(make_tool(tool_id, app_id, "Changed."))).changed
        stored = await repo.get_tool(tool_id, "1.0.0")
        assert stored.description == "Changed." and stored.enabled is False
        assert stored.input_schema["properties"]["n"] == {"type": "number"}
    finally:
        await repo.delete_tool(tool_id, "1.0.0")


async def test_app_upsert_is_idempotent(repo, ids):
    _, app_id = ids
    manifest = {"app_id": app_id, "tools": [], "nested": {"a": [1, 2.5, True, None]}}
    try:
        assert (await repo.upsert_app(app_id, "Test", manifest)).created
        assert not (await repo.upsert_app(app_id, "Test", manifest)).changed
        assert (await repo.upsert_app(app_id, "Test", {**manifest, "x": 1})).changed
        assert (await repo.get_app(app_id)).manifest["x"] == 1
    finally:
        assert await repo.delete_app(app_id)
    assert await repo.get_app(app_id) is None
