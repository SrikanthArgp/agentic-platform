import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import HealthResponse, app, create_app
from tests.conftest import FakeOutbox, FakeRegistry


def test_healthz_returns_200_with_expected_shape():
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ingestion"}


def test_health_response_rejects_missing_fields():
    with pytest.raises(ValidationError):
        HealthResponse(status="ok")


def make_app(apps_dir, outbox):
    return create_app(Settings(apps_dir=apps_dir), outbox=outbox, apps=FakeRegistry())


def test_readyz_is_503_until_startup_has_run(apps_dir):
    # No lifespan yet: uvicorn wouldn't route here either, but a probe must say so.
    response = TestClient(make_app(apps_dir, FakeOutbox())).get("/readyz")
    assert response.status_code == 503
    assert response.json()["reasons"] == ["starting"]


def test_readyz_is_200_once_started_with_postgres_up(apps_dir):
    with TestClient(make_app(apps_dir, FakeOutbox())) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ingestion"}


def test_readyz_is_503_while_postgres_is_down_and_healthz_stays_200(apps_dir):
    with TestClient(make_app(apps_dir, FakeOutbox(fail=True))) as client:
        ready, live = client.get("/readyz"), client.get("/healthz")
    assert ready.status_code == 503
    assert ready.json()["reasons"] == ["outbox: Postgres unreachable (ConnectionRefusedError)"]
    assert live.status_code == 200


def test_readyz_is_503_once_draining(apps_dir):
    app_ = make_app(apps_dir, FakeOutbox())
    with TestClient(app_) as client:
        app_.state.lifecycle.begin_drain()
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["reasons"] == ["draining"]
