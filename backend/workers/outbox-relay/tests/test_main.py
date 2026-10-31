import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.relay import Relay, Role
from tests.conftest import FakeDatabase, FakeProducer


def client_for(role: Role) -> TestClient:
    r = Relay(FakeDatabase(), FakeProducer())
    r.role = role
    return TestClient(create_app(Settings(), relay=r, start=False))


def test_healthz():
    r = client_for(Role.STARTING).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "outbox-relay"}


@pytest.mark.parametrize(
    "role, code", [(Role.ACTIVE, 200), (Role.STANDBY, 200), (Role.STARTING, 503), (Role.DISCONNECTED, 503)]
)
def test_readyz_reflects_the_relays_role(role, code):
    r = client_for(role).get("/readyz")
    assert r.status_code == code
    assert r.json()["role"] == role.value


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("RELAY_POLL_INTERVAL_S", "0.25")
    monkeypatch.setenv("OUTBOX_RETENTION_H", "6")
    s = Settings.from_env()
    assert (s.poll_interval_s, s.retention_h, s.batch_size) == (0.25, 6.0, 100)
