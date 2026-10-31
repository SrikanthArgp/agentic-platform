"""`POST /apps/{app_id}/events`, with a fake outbox and registry."""

import json
from datetime import UTC, datetime

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.config import DEFAULT_APPS_DIR, Settings
from app.main import create_app
from proto_gen import agent_pb2
from tests.conftest import APP_ID, EVENTS_URL, FakeOutbox, FakeRegistry, alert_body, resolved_app


def test_valid_alert_is_written_to_the_outbox_and_accepted(client, outbox):
    response = client.post(EVENTS_URL, json=alert_body())

    assert response.status_code == 202
    body = response.json()
    assert body["app_id"] == APP_ID
    assert body["alert_key"] == "disk_full:web-01"
    assert body["status"] == "accepted"

    [row] = outbox.rows
    assert row.topic == "alert.received"
    assert row.kafka_key == "test-app:disk_full:web-01"
    assert (row.app_id, row.alert_id) == (APP_ID, body["alert_id"])
    message = agent_pb2.RunAgentRequest.FromString(row.payload)
    assert message.alert_id == body["alert_id"]
    assert message.app_id == APP_ID
    assert message.agent_id == ""  # orchestrator resolves the entry agent
    assert (message.source, message.severity) == ("prometheus", "warning")


def test_caller_cannot_set_alert_id_app_id_or_alert_key(client, outbox):
    response = client.post(EVENTS_URL, json=alert_body(alert_id="mine", app_id="other-app", alert_key="x"))
    assert response.status_code == 422
    assert outbox.rows == []


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
def test_bad_envelope_is_rejected_before_the_outbox(client, outbox, overrides):
    response = client.post(EVENTS_URL, json=alert_body(**overrides))
    assert response.status_code == 422
    assert outbox.rows == []


def test_missing_envelope_field_is_rejected(client, outbox):
    body = alert_body()
    del body["source"]
    assert client.post(EVENTS_URL, json=body).status_code == 422
    assert outbox.rows == []


@pytest.mark.parametrize(
    "payload, path",
    [
        ({"host": "web-01"}, "payload"),  # required alert_type
        ({"alert_type": "Disk Full!", "host": "web-01"}, "payload.alert_type"),
        ({"alert_type": "disk_full", "host": ""}, "payload.host"),
        ({"alert_type": "disk_full", "host": "web-01", "value": "high"}, "payload.value"),
    ],
)
def test_payload_failing_app_schema_is_rejected_with_paths(client, outbox, payload, path):
    response = client.post(EVENTS_URL, json=alert_body(payload=payload))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "payload is invalid"
    assert path in [e["path"] for e in detail["errors"]]
    assert outbox.rows == []


def test_oversized_payload_is_rejected(client, outbox):
    payload = {"alert_type": "disk_full", "host": "web-01", "blob": "x" * 2000}
    response = client.post(EVENTS_URL, json=alert_body(payload=payload))
    assert response.status_code == 413
    assert outbox.rows == []


def test_outbox_down_is_503_with_retry_after(client, outbox):
    outbox.fail = True
    response = client.post(EVENTS_URL, json=alert_body())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


def test_explicit_timestamp_is_used(client, outbox):
    client.post(EVENTS_URL, json=alert_body(timestamp="2026-10-03T07:00:00+02:00"))
    message = agent_pb2.RunAgentRequest.FromString(outbox.rows[0].payload)
    assert message.timestamp_unix_ms == int(datetime(2026, 10, 3, 5, 0, tzinfo=UTC).timestamp() * 1000)


def test_real_it_ops_app_accepts_a_realistic_alert():
    """The checked-in manifest's fields against the schema in this image."""
    app_id = "it-ops-triage"
    manifest = yaml.safe_load((DEFAULT_APPS_DIR / app_id / "manifest.yaml").read_text())
    outbox = FakeOutbox()
    client_ = TestClient(create_app(
        Settings(apps_dir=DEFAULT_APPS_DIR), outbox=outbox,
        apps=FakeRegistry({app_id: manifest}),
    ))
    body = alert_body(payload={"alert_type": "disk_full", "host": "web-01", "metric": "disk_used_percent",
                               "value": 97.5, "labels": {"env": "prod"}})
    response = client_.post(f"/apps/{app_id}/events", json=body)

    assert response.status_code == 202, response.text
    assert outbox.rows[0].kafka_key == "it-ops-triage:disk_full:web-01"


COST_APP = "cost-anomaly-triage"
SPIKE = {
    "account": "123456789012",
    "service": "ml-platform",
    "region": "eu-west-1",
    "tags": {"team": "ml"},
    "window_start": "2026-10-05T00:00:00Z",
    "window_end": "2026-10-06T00:00:00Z",
    "baseline_cost": 303.4,
    "observed_cost": 6594.84,
}


def _real_cost_app() -> tuple[TestClient, FakeOutbox]:
    manifest = yaml.safe_load((DEFAULT_APPS_DIR / COST_APP / "manifest.yaml").read_text())
    outbox = FakeOutbox()
    client_ = TestClient(create_app(
        Settings(apps_dir=DEFAULT_APPS_DIR), outbox=outbox,
        apps=FakeRegistry({COST_APP: manifest}),
    ))
    return client_, outbox


def test_real_cost_app_accepts_a_spend_spike_keyed_by_service_and_region():
    client_, outbox = _real_cost_app()
    response = client_.post(f"/apps/{COST_APP}/events", json=alert_body(payload=SPIKE))

    assert response.status_code == 202, response.text
    assert outbox.rows[0].kafka_key == "cost-anomaly-triage:ml-platform:eu-west-1"
    message = agent_pb2.RunAgentRequest.FromString(outbox.rows[0].payload)
    assert (message.app_id, message.alert_key) == (COST_APP, "ml-platform:eu-west-1")


@pytest.mark.parametrize(
    "change",
    [
        {"service": None},
        {"region": None},
        {"observed_cost": None},
        {"window_start": None},
        {"region": "EU-West-1"},
        {"service": "ML Platform"},
        {"observed_cost": -5},
        {"observed_cost": "6594.84"},
        {"window_end": "2026-10-06"},
        {"window_end": "2026-10-06T00:00:00"},
        {"tags": {"team": 5}},
        {"anomaly_level": "huge"},
        {"anomaly_level": 3},
    ],
    ids=lambda c: next(iter(c)) + ("-missing" if next(iter(c.values())) is None else "-invalid"),
)
def test_real_cost_app_rejects_a_malformed_spike(change):
    payload = {**SPIKE, **change}
    payload = {k: v for k, v in payload.items() if v is not None}
    client_, outbox = _real_cost_app()
    response = client_.post(f"/apps/{COST_APP}/events", json=alert_body(payload=payload))

    assert response.status_code == 422, response.text
    assert outbox.rows == []


@pytest.mark.parametrize("level", ["low", "high", "extreme"])
def test_real_cost_app_accepts_each_anomaly_level(level):
    client_, outbox = _real_cost_app()
    response = client_.post(f"/apps/{COST_APP}/events", json=alert_body(payload={**SPIKE, "anomaly_level": level}))

    assert response.status_code == 202, response.text
    message = agent_pb2.RunAgentRequest.FromString(outbox.rows[0].payload)
    assert message.payload["anomaly_level"] == level


def test_an_it_ops_payload_is_rejected_by_the_cost_app():
    client_, outbox = _real_cost_app()
    it_ops = {"alert_type": "disk_full", "host": "web-01"}
    assert client_.post(f"/apps/{COST_APP}/events", json=alert_body(payload=it_ops)).status_code == 422
    assert outbox.rows == []


def test_unknown_app_is_404_before_the_outbox(client, outbox):
    response = client.post("/apps/made-up-app/events", json=alert_body())
    assert response.status_code == 404
    assert "made-up-app" in response.json()["detail"]
    assert outbox.rows == []


def test_app_id_comes_from_the_url(client, outbox, registry, apps_dir):
    (apps_dir / "other-app").mkdir()
    (apps_dir / "other-app" / "event_schema.json").write_text((apps_dir / APP_ID / "event_schema.json").read_text())
    registry.apps["other-app"] = resolved_app("other-app")

    body = client.post("/apps/other-app/events", json=alert_body()).json()

    assert body["app_id"] == "other-app"
    message = agent_pb2.RunAgentRequest.FromString(outbox.rows[0].payload)
    assert message.app_id == "other-app"
    assert outbox.rows[0].kafka_key == "other-app:disk_full:web-01"


def test_payload_valid_for_one_app_is_rejected_under_another(client, outbox, registry, apps_dir):
    (apps_dir / "cost-app").mkdir()
    (apps_dir / "cost-app" / "event_schema.json").write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["service", "region"],
        "properties": {"service": {"type": "string"}, "region": {"type": "string"}},
    }))
    registry.apps["cost-app"] = resolved_app("cost-app", alert_key_fields=["service", "region"])

    assert client.post(EVENTS_URL, json=alert_body()).status_code == 202
    response = client.post("/apps/cost-app/events", json=alert_body())

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "payload is invalid"
    assert len(outbox.rows) == 1


def test_registry_down_is_503_with_retry_after(client, outbox, registry):
    registry.down = True
    response = client.post(EVENTS_URL, json=alert_body())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert outbox.rows == []


def test_alert_key_field_not_in_schema_fails_the_apps_events_explicitly(client, outbox, registry):
    registry.apps[APP_ID] = resolved_app(alert_key_fields=["alert_type", "hostname"])
    response = client.post(EVENTS_URL, json=alert_body())
    assert response.status_code == 500
    assert "misconfigured" in response.json()["detail"]
    assert outbox.rows == []


def test_old_alerts_route_is_gone(client):
    assert client.post("/alerts", json=alert_body()).status_code == 404


def test_missing_alert_key_field_is_422_before_the_outbox(client, outbox, registry):
    # `value` is a schema property but not required: the schema passes, the
    # alert_key can't be built, and the caller gets an explicit 4xx.
    registry.apps[APP_ID] = resolved_app(alert_key_fields=["alert_type", "value"])
    response = client.post(EVENTS_URL, json=alert_body(payload={"alert_type": "disk_full", "host": "web-01"}))

    assert response.status_code == 422
    assert "payload.value" in response.json()["detail"]["message"]
    assert outbox.rows == []


def test_the_requests_trace_context_is_stored_with_the_row(client, outbox):
    """FastAPI opens a server span per request; the relay forwards it."""
    import re

    assert client.post(EVENTS_URL, json=alert_body()).status_code == 202
    [row] = outbox.rows
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", row.headers["traceparent"])
