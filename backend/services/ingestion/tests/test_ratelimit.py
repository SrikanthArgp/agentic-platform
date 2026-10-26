"""Rate limits per app and per source within it (Day 14, ADR-0029)."""

import json

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.ratelimit import Limits, RateLimited, RateLimiter
from app.main import create_app
from tests.conftest import APP_ID, SCHEMA, FakePublisher, FakeRegistry, alert_body, resolved_app

pytestmark = pytest.mark.anyio

OTHER = "other-app"
T0 = 1_793_000_000.0 - (1_793_000_000.0 % 60)  # the start of a minute


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def limiter(redis=None, clock=None, per_minute: int = 5, per_source: int = 3) -> RateLimiter:
    return RateLimiter(
        redis or FakeAsyncRedis(), default_per_minute=per_minute, default_per_source_per_minute=per_source,
        clock=clock or Clock(),
    )


def down_redis() -> FakeAsyncRedis:
    server = FakeServer()
    server.connected = False
    return FakeAsyncRedis(server=server)


async def hits(rl: RateLimiter, n: int, app_id: str = APP_ID, source: str = "prometheus", limits=Limits()) -> list[str]:
    """Each hit's outcome: 'ok', or the scope of the limit it hit."""
    out = []
    for _ in range(n):
        try:
            await rl.hit(app_id, source, limits)
            out.append("ok")
        except RateLimited as e:
            out.append(e.scope)
    return out


# --- RateLimiter ---


async def test_per_source_limit_then_the_sources_share_the_app_limit():
    rl = limiter(per_minute=5, per_source=3)

    assert await hits(rl, 4, source="prometheus") == ["ok", "ok", "ok", "per_source_per_minute"]
    # A second source has its own 3, but only one is left of the app's 5 (the
    # refused 4th event counted too).
    assert await hits(rl, 2, source="datadog") == ["ok", "per_minute"]


async def test_each_app_has_its_own_counters():
    rl = limiter(per_minute=2, per_source=2)
    assert await hits(rl, 3, app_id=APP_ID) == ["ok", "ok", "per_minute"]

    assert await hits(rl, 2, app_id=OTHER) == ["ok", "ok"]


async def test_a_new_minute_is_a_new_window():
    clock = Clock()
    rl = limiter(clock=clock, per_minute=1, per_source=1)
    assert await hits(rl, 2) == ["ok", "per_minute"]

    clock.now += 60
    assert await hits(rl, 1) == ["ok"]


@pytest.mark.parametrize("offset, retry_after", [(0, 60), (0.5, 60), (45, 15), (59.9, 1)])
async def test_retry_after_is_the_seconds_to_the_next_window(offset, retry_after):
    rl = limiter(clock=Clock(T0 + offset), per_minute=1, per_source=1)
    await rl.hit(APP_ID, "s", Limits())

    with pytest.raises(RateLimited) as e:
        await rl.hit(APP_ID, "s", Limits())
    assert e.value.retry_after_s == retry_after


async def test_the_manifests_limits_override_the_defaults():
    rl = limiter(per_minute=100, per_source=100)
    assert await hits(rl, 3, limits=Limits(per_minute=2)) == ["ok", "ok", "per_minute"]
    assert await hits(rl, 2, app_id=OTHER, limits=Limits(per_source_per_minute=1)) == ["ok", "per_source_per_minute"]


async def test_counters_expire():
    redis = FakeAsyncRedis()
    await limiter(redis).hit(APP_ID, "prometheus", Limits())

    keys = sorted(k.decode() for k in await redis.keys("ratelimit:*"))
    window = int(T0 // 60)
    assert keys == [f"ratelimit:{APP_ID}:{window}", f"ratelimit:{APP_ID}:src:prometheus:{window}"]
    for k in keys:
        assert 0 < await redis.ttl(k) <= 120


async def test_redis_down_accepts_the_event():
    assert await hits(limiter(down_redis(), per_minute=1, per_source=1), 3) == ["ok", "ok", "ok"]


def test_limits_from_manifest():
    assert Limits.from_manifest(None) == Limits()
    assert Limits.from_manifest({"per_minute": 10}) == Limits(per_minute=10)


# --- POST /apps/{app_id}/events ---


@pytest.fixture
def two_apps(apps_dir):
    (apps_dir / OTHER).mkdir()
    (apps_dir / OTHER / "event_schema.json").write_text(json.dumps(SCHEMA))

    def make(rate_limit: dict | None = None, rl: RateLimiter | None = None):
        registry = FakeRegistry({APP_ID: resolved_app(rate_limit=rate_limit), OTHER: resolved_app(OTHER)})
        publisher = FakePublisher()
        app = create_app(Settings(apps_dir=apps_dir, kafka_enabled=False), publisher=publisher, apps=registry,
                         rate_limiter=rl or limiter(per_minute=100, per_source=100))
        return TestClient(app), publisher

    return make


def test_flooding_one_app_gets_429_and_the_other_app_is_still_accepted(two_apps):
    client, publisher = two_apps(rate_limit={"per_minute": 3})
    codes = [client.post(f"/apps/{APP_ID}/events", json=alert_body()).status_code for _ in range(5)]
    other = client.post(f"/apps/{OTHER}/events", json=alert_body())

    assert codes == [202, 202, 202, 429, 429]
    assert other.status_code == 202
    assert [k for _, k, _ in publisher.sent].count(b"other-app:disk_full:web-01") == 1
    assert len(publisher.sent) == 4  # nothing past the limit reached Kafka


def test_429_says_which_limit_and_when_to_retry(two_apps):
    client, _ = two_apps(rate_limit={"per_source_per_minute": 1})
    client.post(f"/apps/{APP_ID}/events", json=alert_body())
    response = client.post(f"/apps/{APP_ID}/events", json=alert_body())

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    detail = response.json()["detail"]
    assert detail["limit"] == "per_source_per_minute"
    assert detail["limit_per_minute"] == 1
    assert "source 'prometheus' of app 'test-app'" in detail["message"]
    # Another source of the same app is still accepted.
    assert client.post(f"/apps/{APP_ID}/events", json=alert_body(source="datadog")).status_code == 202


def test_an_unknown_app_is_still_404_not_429(two_apps):
    client, _ = two_apps(rl=limiter(per_minute=1, per_source=1))
    for _ in range(3):
        assert client.post("/apps/made-up-app/events", json=alert_body()).status_code == 404


def test_redis_down_events_are_accepted(two_apps):
    client, publisher = two_apps(rl=limiter(down_redis(), per_minute=1, per_source=1))
    assert [client.post(f"/apps/{APP_ID}/events", json=alert_body()).status_code for _ in range(3)] == [202] * 3
    assert len(publisher.sent) == 3
