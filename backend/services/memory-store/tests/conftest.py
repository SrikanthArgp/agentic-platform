"""Fakes for unit tests: in-memory event store, fake registry, fakeredis."""

from dataclasses import dataclass, field

import pytest
from fakeredis import FakeAsyncRedis

from app.core.events import CONFIRMED_INCIDENT, DECISION, Event, Facts
from app.core.service import MemoryService
from app.redis.cache import MemoryCache
from registry_client import AppNotFoundError, RegistryUnavailableError

APP_ID = "test-app"
NAMESPACE = "test-ns"
KEY = "disk_full:web-01"
NOW = 1_800_000_000_000
H = 3_600_000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class FakeStore:
    rows: dict[tuple[str, str, str], tuple[str, Event]] = field(default_factory=dict)
    loads: int = 0

    async def insert(self, app_id: str, alert_key: str, event: Event) -> bool:
        k = (app_id, event.family, event.ref_id)
        if k in self.rows:
            return False
        self.rows[k] = (alert_key, event)
        return True

    async def load(self, app_id: str, alert_key: str, since_ms: int) -> tuple[list[Event], Facts]:
        self.loads += 1
        mine = [e for (a, _, _), (k, e) in self.rows.items() if a == app_id and k == alert_key]
        return sorted((e for e in mine if e.at_ms > since_ms), key=lambda e: e.at_ms), Facts(
            seen=any(e.family == DECISION for e in mine),
            confirmed_incident=any(e.kind == CONFIRMED_INCIDENT for e in mine),
        )


class FakeApps:
    def __init__(self, apps: dict[str, str] | None = None):
        self.apps = apps if apps is not None else {APP_ID: NAMESPACE}
        self.down = False

    async def get_app(self, app_id: str) -> dict:
        if self.down:
            raise RegistryUnavailableError("down")
        if app_id not in self.apps:
            raise AppNotFoundError(app_id)
        return {"app_id": app_id, "memory_namespace": self.apps[app_id]}


class Clock:
    def __init__(self, now: int = NOW):
        self.now = now

    def __call__(self) -> int:
        return self.now


def decision(kind: str, ref_id: str, hours_ago: float) -> Event:
    return Event(kind=f"decision:{kind}", ref_id=ref_id, at_ms=int(NOW - hours_ago * H))


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def apps() -> FakeApps:
    return FakeApps()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(store, redis, apps, clock) -> MemoryService:
    return MemoryService(store, MemoryCache(redis), apps, clock=clock)
