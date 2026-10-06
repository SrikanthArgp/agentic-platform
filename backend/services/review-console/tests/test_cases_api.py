"""The REST API over the fakes: filters, status codes, verdict bodies."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.core.cases import case_from_decision
from app.main import create_app
from tests.conftest import APP_ID, KEY, decided


@pytest.fixture
def client(store, publisher):
    with TestClient(create_app(store=store, publisher=publisher)) as c:
        yield c


def add(store, **overrides) -> None:
    asyncio.run(store.add(case_from_decision(decided(**overrides))))


def test_list_requires_app_id(client):
    assert client.get("/cases").status_code == 422


def test_list_is_scoped_to_the_app_and_newest_first(client, store):
    add(store, alert_id="a1")
    add(store, alert_id="b1", app_id="other-app")
    add(store, alert_id="a2")
    body = client.get("/cases", params={"app_id": APP_ID}).json()
    assert [c["alert_id"] for c in body["cases"]] == ["a2", "a1"]


def test_list_filters_by_alert_key_and_status(client, store):
    add(store, alert_id="a1")
    add(store, alert_id="a2", alert_key="cpu_high:web-02")
    add(store, alert_id="a3")
    client.post("/cases/3/verdict", json={"verdict": "CONFIRMED_NOISE", "verdict_by": "ana"})

    by_key = client.get("/cases", params={"app_id": APP_ID, "alert_key": KEY}).json()["cases"]
    assert [c["alert_id"] for c in by_key] == ["a3", "a1"]
    open_ = client.get("/cases", params={"app_id": APP_ID, "status": "OPEN"}).json()["cases"]
    assert [c["alert_id"] for c in open_] == ["a2", "a1"]
    page = client.get("/cases", params={"app_id": APP_ID, "limit": 1, "offset": 1}).json()["cases"]
    assert [c["alert_id"] for c in page] == ["a2"]


def test_detail_shows_reasons_tool_calls_and_confidence(client, store):
    add(store)
    body = client.get("/cases/1").json()
    assert body["status"] == "OPEN" and body["decision"] == "ESCALATE"
    assert body["reasons"] == ["disk at 97% on web-01", "runbook says escalate above 95%"]
    assert body["tool_calls"] == [
        {"tool_name": "lookup_runbook", "result_summary": "disk_full: escalate >95%", "agent_id": "triage-agent"},
        {"tool_name": "recent-changes-lookup", "result_summary": "[]", "agent_id": "root-cause-summarizer"},
    ]
    assert body["alert"] == {
        "source": "prometheus",
        "severity": "critical",
        "message": "Disk 97% full on web-01",
        "fired_at": "2026-10-06T12:00:00Z",
        "payload": {"alert_type": "disk_full", "host": "web-01", "value": 97.0},
    }
    assert body["confidence"] == 0.55
    assert body["verdict"] is None


def test_detail_unknown_case_is_404(client):
    assert client.get("/cases/42").status_code == 404


@pytest.mark.parametrize("notes", [None, "rotated logs on web-01"])
def test_verdict_resolves_and_publishes(client, store, publisher, notes):
    add(store)
    body = {"verdict": "CONFIRMED_INCIDENT", "verdict_by": "ana"} | ({"resolution_notes": notes} if notes else {})
    r = client.post("/cases/1/verdict", json=body)
    assert r.status_code == 200
    case = r.json()
    assert (case["status"], case["verdict"], case["resolution_notes"]) == ("RESOLVED", "CONFIRMED_INCIDENT", notes)
    [(topic, key, value)] = publisher.sent
    assert topic == "verdict.recorded" and key == f"{APP_ID}:{KEY}".encode()
    assert json.loads(value)["case_id"] == 1


def test_second_verdict_is_409_with_the_existing_one(client, store):
    add(store)
    client.post("/cases/1/verdict", json={"verdict": "CONFIRMED_NOISE", "verdict_by": "ana"})
    r = client.post("/cases/1/verdict", json={"verdict": "CONFIRMED_INCIDENT", "verdict_by": "bo"})
    assert r.status_code == 409
    assert r.json()["detail"]["verdict"] == "CONFIRMED_NOISE"
    assert client.get("/cases/1").json()["verdict_by"] == "ana"


def test_verdict_on_unknown_case_is_404(client):
    r = client.post("/cases/42/verdict", json={"verdict": "CONFIRMED_NOISE", "verdict_by": "ana"})
    assert r.status_code == 404


def test_kafka_down_is_503_and_the_case_stays_open(client, store, publisher):
    add(store)
    publisher.down = True
    r = client.post("/cases/1/verdict", json={"verdict": "CONFIRMED_NOISE", "verdict_by": "ana"})
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert client.get("/cases/1").json()["status"] == "OPEN"


@pytest.mark.parametrize(
    "body",
    [
        {"verdict": "MAYBE", "verdict_by": "ana"},
        {"verdict": "CONFIRMED_NOISE"},
        {"verdict": "CONFIRMED_NOISE", "verdict_by": "   "},
        {"verdict": "CONFIRMED_NOISE", "verdict_by": "ana", "resolution_notes": "x" * 4001},
    ],
    ids=["bad-verdict", "no-verdict_by", "blank-verdict_by", "notes-too-long"],
)
def test_invalid_verdict_body_is_422(client, store, publisher, body):
    add(store)
    assert client.post("/cases/1/verdict", json=body).status_code == 422
    assert publisher.sent == []


def test_blank_notes_are_stored_as_none(client, store):
    add(store)
    r = client.post("/cases/1/verdict", json={"verdict": "CONFIRMED_NOISE", "verdict_by": " ana ", "resolution_notes": "  "})
    assert (r.json()["resolution_notes"], r.json()["verdict_by"]) == (None, "ana")

