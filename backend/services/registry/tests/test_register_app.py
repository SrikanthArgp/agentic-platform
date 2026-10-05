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


@pytest.fixture
def served():
    return {
        "lookup_runbook": {
            "description": "Look up the runbook.",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "version": "1.0.0",
            "scope": "app",
            "app_id": APP_ID,
            "read_only": True,
        }
    }


def test_checked_in_manifest_registers(client, manifest, served):
    assert register_app.register(manifest, served, client) == [
        "tool lookup_runbook 1.0.0: created",
        f"app {APP_ID}: created",
    ]
    app = client.get(f"/apps/{APP_ID}").json()
    assert [t["tool_id"] for t in app["agents"][0]["tools"]] == ["lookup_runbook"]


def test_same_file_twice_changes_nothing(client, manifest, served):
    register_app.register(manifest, served, client)
    before = client.get(f"/apps/{APP_ID}").json()

    assert register_app.register(manifest, served, client) == [
        "tool lookup_runbook 1.0.0: unchanged",
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
