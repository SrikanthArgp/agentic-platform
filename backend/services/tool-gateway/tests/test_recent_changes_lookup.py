"""it-ops-triage's `recent-changes-lookup`, loaded from the real backend/apps
the way tool-gateway loads it at startup (plan.md Day 8)."""

import pytest
from pydantic import ValidationError

from app.core.loader import load_app_tools
from app.core.registry import ToolRegistry
from tests.conftest import REPO_APPS_DIR

pytestmark = pytest.mark.anyio

# Fixture: argocd:checkout-svc@v2.3.1 at 09:40Z on hosts app-03/app-04;
# checkout-svc@v2.3.0 on 10-03; cmdb:CHG-20931 web-01 at 08:15Z;
# cmdb:CHG-20950 web-01/web-02 at 11:00Z.


@pytest.fixture(scope="module")
def tool():
    registry = ToolRegistry()
    load_app_tools(REPO_APPS_DIR, registry)
    return registry.get("recent-changes-lookup")


async def lookup(tool, target, start, end):
    out = await tool.invoke({"service_or_host": target, "window_start": start, "window_end": end})
    return out.model_dump(mode="json")


def test_registered_as_a_read_only_it_ops_triage_tool(tool):
    assert (tool.scope, tool.app_id, tool.version, tool.read_only) == ("app", "it-ops-triage", "1.0.0", True)
    assert tool.input_schema()["required"] == ["service_or_host", "window_start", "window_end"]


@pytest.mark.parametrize(
    "target, start, end, refs",
    [
        # Inside the window, by service and by host.
        ("checkout-svc", "2026-10-05T08:00:00Z", "2026-10-05T10:00:00Z", ["argocd:checkout-svc@v2.3.1"]),
        ("app-03", "2026-10-05T08:00:00Z", "2026-10-05T10:00:00Z", ["argocd:checkout-svc@v2.3.1"]),
        # Outside the window (after it / before it).
        ("checkout-svc", "2026-10-05T07:00:00Z", "2026-10-05T09:39:59Z", []),
        ("checkout-svc", "2026-10-05T09:40:01Z", "2026-10-05T12:00:00Z", []),
        # On the edges: both ends are included.
        ("checkout-svc", "2026-10-05T09:40:00Z", "2026-10-05T10:00:00Z", ["argocd:checkout-svc@v2.3.1"]),
        ("checkout-svc", "2026-10-05T08:00:00Z", "2026-10-05T09:40:00Z", ["argocd:checkout-svc@v2.3.1"]),
        # Offsets other than Z compare as instants: 11:40+02:00 is 09:40Z.
        ("checkout-svc", "2026-10-05T11:00:00+02:00", "2026-10-05T11:40:00+02:00", ["argocd:checkout-svc@v2.3.1"]),
        # Several matches, most recent first.
        ("checkout-svc", "2026-10-03T00:00:00Z", "2026-10-06T00:00:00Z",
         ["argocd:checkout-svc@v2.3.1", "argocd:checkout-svc@v2.3.0"]),
        ("web-01", "2026-10-05T00:00:00Z", "2026-10-05T12:00:00Z", ["cmdb:CHG-20950", "cmdb:CHG-20931"]),
        # Nothing for this target at all.
        ("billing-svc", "2026-10-01T00:00:00Z", "2026-10-06T00:00:00Z", []),
        # A host is matched exactly, not by prefix.
        ("app-0", "2026-10-05T08:00:00Z", "2026-10-05T10:00:00Z", []),
    ],
)
async def test_window_filtering(tool, target, start, end, refs):
    out = await lookup(tool, target, start, end)
    assert [c["ref"] for c in out["changes"]] == refs
    assert out["found"] is bool(refs)


async def test_no_changes_found_says_so(tool):
    out = await lookup(tool, "checkout-svc", "2026-10-05T10:00:00Z", "2026-10-05T12:00:00Z")
    assert out == {
        "service_or_host": "checkout-svc",
        "window_start": "2026-10-05T10:00:00Z",
        "window_end": "2026-10-05T12:00:00Z",
        "found": False,
        "changes": [],
        "message": "No recorded changes to 'checkout-svc' in this window.",
    }


async def test_a_change_has_everything_a_reason_cites(tool):
    [change] = (await lookup(tool, "app-03", "2026-10-05T08:00:00Z", "2026-10-05T10:00:00Z"))["changes"]
    assert change == {
        "ref": "argocd:checkout-svc@v2.3.1",
        "timestamp": "2026-10-05T09:40:00Z",
        "type": "deploy",
        "service": "checkout-svc",
        "hosts": ["app-03", "app-04"],
        "author": "deploy-bot (PR #4812 by a.rivera)",
        "summary": "Deploy checkout-svc v2.3.1: new payment-provider client, connection pool size 50 -> 10.",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"service_or_host": "web-01", "window_start": "2026-10-05T08:00:00Z"},
        {"service_or_host": "", "window_start": "2026-10-05T08:00:00Z", "window_end": "2026-10-05T10:00:00Z"},
        # No UTC offset.
        {"service_or_host": "web-01", "window_start": "2026-10-05T08:00:00", "window_end": "2026-10-05T10:00:00"},
        {"service_or_host": "web-01", "window_start": "yesterday", "window_end": "now"},
        # End before start; longer than 7 days.
        {"service_or_host": "web-01", "window_start": "2026-10-05T10:00:00Z", "window_end": "2026-10-05T08:00:00Z"},
        {"service_or_host": "web-01", "window_start": "2026-09-01T00:00:00Z", "window_end": "2026-10-05T00:00:00Z"},
        {"service_or_host": "web-01", "window_start": "2026-10-05T08:00:00Z", "window_end": "2026-10-05T10:00:00Z",
         "app_id": "other-app"},
    ],
)
async def test_rejects_invalid_input(tool, arguments):
    with pytest.raises(ValidationError):
        await tool.invoke(arguments)
