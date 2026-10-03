"""`POST /alerts` and the debug `GET /alerts/{id}`, with a fake Kafka publisher."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import DEFAULT_APPS_DIR, Settings
from app.main import create_app
from proto_gen import agent_pb2
from tests.conftest import APP_ID, FakePublisher, alert_body


def test_valid_alert_is_published_and_accepted(client, publisher):
    response = client.post("/alerts", json=alert_body())

    assert response.status_code == 202
    body = response.json()
    assert body["app_id"] == APP_ID
    assert body["alert_key"] == "disk_full:web-01"
    assert body["status"] == "accepted"

    [(topic, key, value)] = publisher.sent
    assert topic == "alert.received"
    assert key == b"test-app:disk_full:web-01"
    message = agent_pb2.RunAgentRequest.FromString(value)
    assert message.alert_id == body["alert_id"]
    assert message.app_id == APP_ID
    assert message.agent_id == ""  # orchestrator resolves the entry agent
    assert (message.source, message.severity) == ("prometheus", "warning")


def test_caller_cannot_set_alert_id_app_id_or_alert_key(client, publisher):
    response = client.post("/alerts", json=alert_body(alert_id="mine", app_id="other-app", alert_key="x"))
    assert response.status_code == 422
    assert publisher.sent == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": ""},
        {"severity": "Very Bad!"},
        {"message": ""},
        {"timestamp": "yesterday"},
        {"timestamp": "2026-10-03T07:00:00"},  # no UTC offset
        {"payload": "disk_full"},
    ],
)
def test_bad_envelope_is_rejected_before_kafka(client, publisher, overrides):
    response = client.post("/alerts", json=alert_body(**overrides))
    assert response.status_code == 422
    assert publisher.sent == []


def test_missing_envelope_field_is_rejected(client, publisher):
    body = alert_body()
    del body["source"]
    assert client.post("/alerts", json=body).status_code == 422
    assert publisher.sent == []


@pytest.mark.parametrize(
    "payload, path",
    [
        ({"host": "web-01"}, "payload"),  # required alert_type
        ({"alert_type": "Disk Full!", "host": "web-01"}, "payload.alert_type"),
        ({"alert_type": "disk_full", "host": ""}, "payload.host"),
        ({"alert_type": "disk_full", "host": "web-01", "value": "high"}, "payload.value"),
    ],
)
def test_payload_failing_app_schema_is_rejected_with_paths(client, publisher, payload, path):
    response = client.post("/alerts", json=alert_body(payload=payload))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "payload is invalid"
    assert path in [e["path"] for e in detail["errors"]]
    assert publisher.sent == []


def test_oversized_payload_is_rejected(client, publisher):
    payload = {"alert_type": "disk_full", "host": "web-01", "blob": "x" * 2000}
    response = client.post("/alerts", json=alert_body(payload=payload))
    assert response.status_code == 413
    assert publisher.sent == []


def test_kafka_down_is_503_with_retry_after_and_not_tracked(client, publisher):
    publisher.fail = True
    response = client.post("/alerts", json=alert_body())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert client.app.state.tracker._alerts == {}


def test_explicit_timestamp_is_used(client, publisher):
    client.post("/alerts", json=alert_body(timestamp="2026-10-03T07:00:00+02:00"))
    message = agent_pb2.RunAgentRequest.FromString(publisher.sent[0][2])
    assert message.timestamp_unix_ms == int(datetime(2026, 10, 3, 5, 0, tzinfo=UTC).timestamp() * 1000)


def test_debug_get_shows_pending_then_decided_with_latency(client):
    alert_id = client.post("/alerts", json=alert_body()).json()["alert_id"]

    pending = client.get(f"/alerts/{alert_id}").json()
    assert pending["status"] == "pending"
    assert pending["alert_key"] == "disk_full:web-01"

    decided = agent_pb2.RunAgentResponse(
        app_id=APP_ID,
        agent_id="triage-agent",
        alert_id=alert_id,
        decision=agent_pb2.ESCALATE,
        reasons=["RB-001: above 95%"],
        tool_calls=[agent_pb2.ToolCall(tool_name="lookup_runbook", result_summary="{...}")],
    )
    client.app.state.tracker.handle_message(decided.SerializeToString())

    body = client.get(f"/alerts/{alert_id}").json()
    assert body["status"] == "decided"
    assert body["decision"] == "ESCALATE"
    assert body["reasons"] == ["RB-001: above 95%"]
    assert body["tool_calls"] == [{"tool_name": "lookup_runbook", "result_summary": "{...}"}]
    assert body["latency_ms"] >= 0


def test_debug_get_unknown_alert_is_404(client):
    assert client.get("/alerts/nope").status_code == 404


def test_real_it_ops_app_accepts_a_realistic_alert():
    publisher = FakePublisher()
    client_ = TestClient(create_app(Settings(apps_dir=DEFAULT_APPS_DIR, kafka_enabled=False), publisher=publisher))
    body = alert_body(payload={"alert_type": "disk_full", "host": "web-01", "metric": "disk_used_percent",
                               "value": 97.5, "labels": {"env": "prod"}})
    response = client_.post("/alerts", json=body)

    assert response.status_code == 202, response.text
    assert publisher.sent[0][1] == b"it-ops-triage:disk_full:web-01"
