"""`backend/scripts/register_app.py` against an in-memory registry.

tool-gateway's listing is given as data, so no MCP server is needed.
"""

import importlib.util
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]

_spec = importlib.util.spec_from_file_location("register_app", BACKEND / "scripts" / "register_app.py")
register_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_app)

APP_ID = "it-ops-triage"


@pytest.fixture
def manifest():
    return register_app.load_manifest(APP_ID)


def _served_tool(description: str) -> dict:
    return {
        "description": description,
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "version": "1.0.0",
        "scope": "app",
        "app_id": APP_ID,
        "read_only": True,
    }


@pytest.fixture
def served():
    """What tool-gateway serves for it-ops-triage: both its tools."""
    return {
        "lookup_runbook": _served_tool("Look up the runbook."),
        "recent-changes-lookup": _served_tool("Recent changes."),
    }


def test_checked_in_manifest_registers(client, manifest, served):
    assert register_app.register(manifest, served, client) == [
        "tool lookup_runbook 1.0.0: created",
        "tool recent-changes-lookup 1.0.0: created",
        f"app {APP_ID}: created",
    ]
    app = client.get(f"/apps/{APP_ID}").json()
    tools = {a["agent_id"]: [t["tool_id"] for t in a["tools"]] for a in app["agents"]}
    assert tools == {
        "triage-agent": ["lookup_runbook"],
        "root-cause-summarizer": ["lookup_runbook", "recent-changes-lookup"],
    }


def test_same_file_twice_changes_nothing(client, manifest, served):
    register_app.register(manifest, served, client)
    before = client.get(f"/apps/{APP_ID}").json()

    assert register_app.register(manifest, served, client) == [
        "tool lookup_runbook 1.0.0: unchanged",
        "tool recent-changes-lookup 1.0.0: unchanged",
        f"app {APP_ID}: unchanged",
    ]
    assert client.get(f"/apps/{APP_ID}").json() == before


def test_tool_not_served_by_tool_gateway_fails_before_registering(client, manifest):
    with pytest.raises(register_app.RegistrationError, match="Roll out the app's code"):
        register_app.register(manifest, {}, client)
    assert client.get(f"/apps/{APP_ID}").status_code == 404


def test_tool_version_mismatch_fails(client, manifest, served):
    served["lookup_runbook"]["version"] = "0.9.0"
    with pytest.raises(register_app.RegistrationError, match="serves version 0.9.0"):
        register_app.register(manifest, served, client)


def test_tool_not_declared_read_only_is_refused_by_registry(client, manifest, served):
    del served["lookup_runbook"]["read_only"]
    with pytest.raises(register_app.RegistrationError, match="read_only"):
        register_app.register(manifest, served, client)


def test_invalid_manifest_reports_registry_errors(client, manifest, served):
    manifest["alert_key_fields"] = []
    with pytest.raises(register_app.RegistrationError, match="alert_key_fields"):
        register_app.register(manifest, served, client)


def test_a_tool_only_one_agent_declares_is_still_required(client, manifest, served):
    """recent-changes-lookup is the summarizer's alone; registering without it still fails."""
    del served["recent-changes-lookup"]
    with pytest.raises(register_app.RegistrationError, match="serves nothing for 'recent-changes-lookup'"):
        register_app.register(manifest, served, client)
