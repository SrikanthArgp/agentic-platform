import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.repository import InMemoryRepository
from app.main import create_app

APP_ID = "test-app"


def tool_body(app_id: str | None = APP_ID, **overrides: Any) -> dict[str, Any]:
    body = {
        "description": "Look up the runbook for an alert type.",
        "scope": "app" if app_id else "global",
        "app_id": app_id,
        "input_schema": {"type": "object", "properties": {"alert_type": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "read_only": True,
    }
    body.update(overrides)
    return body


MANIFEST: dict[str, Any] = {
    "app_id": APP_ID,
    "display_name": "Test app",
    "agents": [
        {
            "agent_id": "triage-agent",
            "version": "0.1.0",
            "role": "entry",
            "prompt_ref": "prompts/triage-agent.md",
            "tool_allowlist": ["lookup_runbook"],
        },
        {
            "agent_id": "summarizer",
            "version": "0.1.0",
            "role": "callable",
            "prompt_ref": "prompts/summarizer.md",
            "tool_allowlist": [],
            "invoke_on": ["ESCALATE"],
        },
    ],
    "tools": [{"tool_id": "lookup_runbook", "version": "1.0.0", "scope": "app"}],
    "event_schema_ref": "event_schema.json",
    "alert_key_fields": ["alert_type", "host"],
    "memory_namespace": APP_ID,
    "escalate_when": [
        {"field": "alert.severity", "in": ["critical"]},
        {"field": "context.has_confirmed_incident_history", "in": [True]},
    ],
}


def manifest(**overrides: Any) -> dict[str, Any]:
    m = copy.deepcopy(MANIFEST)
    m.update(overrides)
    return m


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app(InMemoryRepository())) as c:
        yield c


@pytest.fixture
def registered(client: TestClient) -> TestClient:
    """A client with `lookup_runbook` 1.0.0 registered for APP_ID."""
    assert client.put("/tools/lookup_runbook/versions/1.0.0", json=tool_body()).status_code == 201
    return client
