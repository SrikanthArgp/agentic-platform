"""`similar-past-case-lookup`, the first global tool (Day 13, ADR-0027)."""

import httpx
import pytest
from mcp import Client

from app.core.loader import load_app_tools
from app.core.registry import DuplicateToolError, ToolRegistry
from app.mcp.server import build_mcp_server
from app.tools.similar_past_cases import TOOL_ID, ReviewConsoleCases, build_tool
from run_context import RunContext
from tests.conftest import FakeApps, FakeCases, echo_module, write_tool_module

pytestmark = pytest.mark.anyio

KEY = "disk_full:web-01"


def case(id: int, app_id: str = "app-one", alert_key: str = KEY, **extra) -> dict:
    return {
        "id": id,
        "app_id": app_id,
        "alert_key": alert_key,
        "decision": "ESCALATE",
        "reasons": [f"reason for case {id}"],
        "verdict": "CONFIRMED_INCIDENT",
        "resolution_notes": None,
        "created_at": "2026-10-05T09:00:00Z",
        "resolved_at": "2026-10-05T10:00:00Z",
        **extra,
    }


ROWS = [
    case(3, resolution_notes="Rotated logs on /var; freed 40 GB."),
    case(2, verdict="CONFIRMED_NOISE"),
    case(9, app_id="app-two"),
    case(8, alert_key="disk_full:web-02"),
]


def server(cases: FakeCases, allowed: dict | None = None):
    registry = ToolRegistry([build_tool(cases)])
    allowed = allowed or {"app-one": {"triage-agent": [TOOL_ID]}, "app-two": {"triage-agent": [TOOL_ID]}}
    return build_mcp_server(registry, FakeApps(allowed, versions={TOOL_ID: "1.0.0"}))


def meta(app_id: str = "app-one", alert_key: str = KEY) -> dict:
    return RunContext(app_id=app_id, agent_id="triage-agent", alert_id="a-1", alert_key=alert_key).to_meta()


async def call(cases: FakeCases, arguments: dict, **context):
    async with Client(server(cases)) as client:
        return await client.call_tool(TOOL_ID, arguments, meta=meta(**context))


async def test_returns_the_runs_own_resolved_cases_with_resolution_notes():
    cases = FakeCases(ROWS)
    result = await call(cases, {})

    assert not result.is_error
    out = result.structured_content
    assert out["found"] is True
    assert out["alert_key"] == KEY
    assert [c["case_id"] for c in out["cases"]] == [3, 2]
    assert out["cases"][0]["resolution_notes"] == "Rotated logs on /var; freed 40 GB."
    assert out["cases"][0]["verdict"] == "CONFIRMED_INCIDENT"
    assert out["cases"][0]["reasons"] == ["reason for case 3"]
    assert out["cases"][1]["resolution_notes"] is None
    assert cases.queries == [("app-one", KEY, 3)]


async def test_scope_comes_from_the_run_context_for_each_app():
    cases = FakeCases(ROWS)
    out = (await call(cases, {}, app_id="app-two")).structured_content

    assert [c["case_id"] for c in out["cases"]] == [9]
    assert cases.queries == [("app-two", KEY, 3)]


@pytest.mark.parametrize(
    "arguments",
    [{"app_id": "app-two"}, {"alert_key": "disk_full:web-02"}, {"limit": 3, "app_id": "app-two"}],
)
async def test_arguments_naming_another_app_or_key_are_refused_and_never_query(arguments):
    cases = FakeCases(ROWS)
    result = await call(cases, arguments)

    assert result.structured_content["error"] == "invalid_arguments"
    assert cases.queries == []


async def test_cases_for_another_app_or_key_are_dropped_even_if_review_console_returns_them(caplog):
    caplog.set_level("WARNING")
    out = (await call(FakeCases(ROWS, unfiltered=True), {"limit": 10})).structured_content

    assert [c["case_id"] for c in out["cases"]] == [3, 2]
    assert "dropped case 9" in caplog.text
    assert "dropped case 8" in caplog.text


async def test_limit_is_passed_and_bounded():
    cases = FakeCases(ROWS)
    out = (await call(cases, {"limit": 1})).structured_content
    assert [c["case_id"] for c in out["cases"]] == [3]
    assert cases.queries == [("app-one", KEY, 1)]

    assert (await call(cases, {"limit": 11})).structured_content["error"] == "invalid_arguments"


async def test_no_cases_is_found_false_not_an_error():
    result = await call(FakeCases([]), {})

    assert not result.is_error
    assert result.structured_content["found"] is False


async def test_a_run_without_alert_key_looks_nothing_up():
    cases = FakeCases(ROWS)
    out = (await call(cases, {}, alert_key="")).structured_content

    assert out["found"] is False
    assert cases.queries == []


async def test_review_console_down_is_tool_failed():
    assert (await call(FakeCases([], down=True), {})).structured_content["error"] == "tool_failed"


async def test_listed_as_a_global_tool():
    async with Client(server(FakeCases([]))) as client:
        [tool] = (await client.list_tools()).tools

    assert tool.name == TOOL_ID
    # A None app_id is dropped from _meta; register_app.py reads it with .get().
    assert tool.meta == {"version": "1.0.0", "scope": "global", "read_only": True}
    assert tool.input_schema["properties"].keys() == {"limit"}


def test_an_app_tool_with_the_global_tools_id_fails_startup(tmp_path):
    write_tool_module(tmp_path, "app-one", "clash", echo_module().replace('"echo"', f'"{TOOL_ID}"'))
    registry = ToolRegistry([build_tool(FakeCases([]))])
    with pytest.raises(Exception, match=TOOL_ID) as e:
        load_app_tools(tmp_path, registry)
    assert isinstance(e.value.__cause__, DuplicateToolError)


async def test_review_console_client_queries_resolved_cases_for_the_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"cases": [case(1)]})

    client = ReviewConsoleCases("http://review-console:8000/", transport=httpx.MockTransport(handler))
    rows = await client.resolved_cases("app-one", KEY, 5)
    await client.aclose()

    assert [r["id"] for r in rows] == [1]
    [url] = seen
    assert url.path == "/cases"
    assert dict(url.params) == {"app_id": "app-one", "alert_key": KEY, "status": "RESOLVED", "limit": "5"}


async def test_review_console_error_status_raises():
    client = ReviewConsoleCases("http://rc", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(httpx.HTTPStatusError):
        await client.resolved_cases("app-one", KEY, 3)
    await client.aclose()
