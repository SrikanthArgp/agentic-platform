import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.kafka.publisher import PublishError
from app.main import create_app
from registry_client import AppNotFoundError, RegistryUnavailableError

APP_ID = "test-app"
EVENTS_URL = f"/apps/{APP_ID}/events"

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["alert_type", "host"],
    "properties": {
        "alert_type": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
        "host": {"type": "string", "minLength": 1},
        "value": {"type": "number"},
    },
    "additionalProperties": True,
}


@pytest.fixture
def apps_dir(tmp_path: Path) -> Path:
    app_dir = tmp_path / APP_ID
    app_dir.mkdir()
    (app_dir / "event_schema.json").write_text(json.dumps(SCHEMA))
    return tmp_path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def resolved_app(app_id: str = APP_ID, **overrides) -> dict:
    """The fields of `GET /apps/{app_id}` ingestion reads."""
    return {
        "app_id": app_id,
        "event_schema_ref": "event_schema.json",
        "alert_key_fields": ["alert_type", "host"],
        **overrides,
    }


class FakeRegistry:
    def __init__(self, apps: dict[str, dict] | None = None):
        self.apps = apps if apps is not None else {APP_ID: resolved_app()}
        self.down = False

    async def get_app(self, app_id: str) -> dict:
        if self.down:
            raise RegistryUnavailableError("connection refused")
        if app_id not in self.apps:
            raise AppNotFoundError(app_id)
        return self.apps[app_id]


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@dataclass
class FakePublisher:
    sent: list[tuple[str, bytes, bytes]] = field(default_factory=list)
    fail: bool = False

    async def publish(self, topic: str, key: bytes, value: bytes) -> None:
        if self.fail:
            raise PublishError("Kafka down")
        self.sent.append((topic, key, value))


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def client(apps_dir: Path, publisher: FakePublisher, registry: FakeRegistry) -> TestClient:
    settings = Settings(apps_dir=apps_dir, kafka_enabled=False, max_payload_bytes=1024)
    return TestClient(create_app(settings, publisher=publisher, apps=registry))


def alert_body(**overrides) -> dict:
    body = {
        "source": "prometheus",
        "severity": "warning",
        "message": "Disk usage at 91% on web-01",
        "payload": {"alert_type": "disk_full", "host": "web-01", "value": 91},
    }
    body.update(overrides)
    return body
