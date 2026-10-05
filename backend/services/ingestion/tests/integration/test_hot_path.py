"""The hot path end to end, against the running Compose stack with
it-ops-triage registered (`register_app.py`):
POST /apps/it-ops-triage/events -> Kafka -> orchestrator -> tool-gateway -> alert.decided ->
GET /alerts/{id}.

Calls the real LLM (a few cents per run). Run with
`uv run pytest -m integration` while `backend/local` is up.
"""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("INGESTION_URL", "http://localhost:8001")
DECISION_TIMEOUT_S = 90


def _post(body: dict, app_id: str = "it-ops-triage") -> httpx.Response:
    return httpx.post(f"{BASE_URL}/apps/{app_id}/events", json=body, timeout=10)


def _wait_decided(alert_id: str) -> dict:
    deadline = time.monotonic() + DECISION_TIMEOUT_S
    while time.monotonic() < deadline:
        body = httpx.get(f"{BASE_URL}/alerts/{alert_id}", timeout=5).json()
        if body["status"] == "decided":
            return body
        time.sleep(0.5)
    pytest.fail(f"no decision for {alert_id} within {DECISION_TIMEOUT_S}s")


def test_alert_is_decided_end_to_end_with_reasons_from_the_tool_trace():
    response = _post(
        {
            "source": "prometheus",
            "severity": "critical",
            "message": "checkout is down on app-03",
            "payload": {"alert_type": "service_down", "host": "app-03", "service": "checkout"},
        }
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["alert_key"] == "service_down:app-03"

    decided = _wait_decided(accepted["alert_id"])

    # RB-003 says a full outage is always escalated.
    assert decided["decision"] == "ESCALATE"
    assert decided["agent_id"] == "triage-agent"
    assert decided["reasons"]
    assert "lookup_runbook" in [c["tool_name"] for c in decided["tool_calls"]]
    assert decided["latency_ms"] > 0
    print(f"\nend-to-end latency: {decided['latency_ms']} ms")


def test_invalid_payload_is_rejected_by_the_running_service():
    response = _post(
        {"source": "prometheus", "severity": "warning", "message": "x", "payload": {"alert_type": "disk_full"}}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["message"] == "'host' is a required property"


def test_made_up_app_is_rejected():
    response = _post({"source": "x", "severity": "warning", "message": "x", "payload": {}}, app_id="made-up-app")
    assert response.status_code == 404
