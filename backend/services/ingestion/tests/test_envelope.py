"""alert_key construction, Kafka key, and the RunAgentRequest round trip."""

from datetime import UTC, datetime

import pytest
from google.protobuf.json_format import MessageToDict

from app.core.apps import AppConfigError, FileAppStore, UnknownAppError
from app.core.envelope import AlertKeyError, build_alert_key, build_request, message_key
from app.models.alerts import AlertIn
from proto_gen import agent_pb2
from tests.conftest import APP_ID


def test_alert_key_joins_fields_in_manifest_order():
    payload = {"host": "web-01", "alert_type": "disk_full", "extra": "ignored"}
    assert build_alert_key(payload, ("alert_type", "host")) == "disk_full:web-01"
    assert build_alert_key(payload, ("host", "alert_type")) == "web-01:disk_full"


def test_alert_key_accepts_numbers_and_bools():
    assert build_alert_key({"a": 7, "b": True}, ("a", "b")) == "7:true"


@pytest.mark.parametrize("payload", [{"alert_type": "x"}, {"alert_type": "x", "host": ""},
                                     {"alert_type": "x", "host": None}, {"alert_type": "x", "host": ["a"]}])
def test_alert_key_missing_or_unusable_field_is_an_error(payload):
    with pytest.raises(AlertKeyError, match="payload.host"):
        build_alert_key(payload, ("alert_type", "host"))


def test_message_key_is_app_id_and_alert_key():
    assert message_key("it-ops-triage", "disk_full:web-01") == b"it-ops-triage:disk_full:web-01"


def test_request_round_trips_through_kafka_bytes_with_nested_payload():
    payload = {
        "alert_type": "disk_full",
        "host": "web-01",
        "value": 97,
        "labels": {"env": "prod", "team": "infra"},
        "mounts": ["/", "/var"],
        "flags": {"rising": True, "acked": None},
    }
    alert = AlertIn(source="prometheus", severity="critical", message="disk", payload=payload)
    received_at = datetime(2026, 10, 3, 7, 0, tzinfo=UTC)

    request = build_request(app_id=APP_ID, alert_id="a-1", alert_key="disk_full:web-01", alert=alert,
                            received_at=received_at)
    decoded = agent_pb2.RunAgentRequest.FromString(request.SerializeToString())

    assert decoded == request
    assert decoded.timestamp_unix_ms == int(received_at.timestamp() * 1000)
    back = MessageToDict(decoded.payload)
    # Struct numbers are doubles (ADR-0007): 97 comes back as 97.0.
    assert back == {**payload, "value": 97.0}
    assert isinstance(back["value"], float)


def test_app_store_loads_spec_and_rejects_unknown_or_bad_apps(apps_dir):
    store = FileAppStore(apps_dir)
    assert store.get(APP_ID).alert_key_fields == ("alert_type", "host")
    for bad in ("no-such-app", "../etc", "Bad"):
        with pytest.raises(UnknownAppError):
            store.get(bad)

    (apps_dir / "broken").mkdir()
    (apps_dir / "broken" / "manifest.yaml").write_text("app_id: broken\nevent_schema_ref: ../x.json\nalert_key_fields: [a]\n")
    with pytest.raises(AppConfigError, match="event_schema_ref"):
        store.get("broken")
