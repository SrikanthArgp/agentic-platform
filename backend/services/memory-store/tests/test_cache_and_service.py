"""MemoryCache (fakeredis) and MemoryService: miss -> Postgres -> refill, record."""

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.events import CONFIRMED_INCIDENT, Event, Facts
from app.redis.cache import MemoryCache
from registry_client import AppNotFoundError, RegistryUnavailableError
from tests.conftest import APP_ID, H, KEY, NAMESPACE, NOW, decision

pytestmark = pytest.mark.anyio


# ----------------------------------------------------------------- cache


async def test_unloaded_key_is_a_miss_even_with_events(redis):
    cache = MemoryCache(redis)
    await cache.add(NAMESPACE, KEY, decision("ESCALATE", "a", 1), NOW)
    assert await cache.read(NAMESPACE, KEY, NOW) is None


async def test_fill_then_read_round_trips(redis):
    cache = MemoryCache(redis)
    events = [decision("ESCALATE", "a", 1), decision("SUPPRESS", "b", 30)]
    await cache.fill(NAMESPACE, KEY, events, Facts(seen=True))
    got_events, facts = await cache.read(NAMESPACE, KEY, NOW)
    assert sorted(got_events, key=lambda e: e.ref_id) == events
    assert facts == Facts(seen=True)


async def test_read_trims_events_older_than_seven_days(redis):
    cache = MemoryCache(redis)
    await cache.fill(NAMESPACE, KEY, [decision("SUPPRESS", "old", 24 * 7), decision("SUPPRESS", "new", 1)], Facts(seen=True))
    events, _ = await cache.read(NAMESPACE, KEY, NOW)
    assert [e.ref_id for e in events] == ["new"]
    assert await redis.zcard(f"mem:{NAMESPACE}:{KEY}:events") == 1


async def test_keys_are_namespaced_and_expire(redis):
    cache = MemoryCache(redis, ttl_s=100)
    await cache.fill(NAMESPACE, KEY, [decision("SUPPRESS", "a", 1)], Facts(seen=True))
    assert sorted(await redis.keys("*")) == [f"mem:{NAMESPACE}:{KEY}:events", f"mem:{NAMESPACE}:{KEY}:facts"]
    assert 0 < await redis.ttl(f"mem:{NAMESPACE}:{KEY}:facts") <= 100


async def test_a_rebuild_never_clears_a_fact_a_writer_set(redis):
    """Writer adds (seen) between a rebuild's Postgres read and its fill."""
    cache = MemoryCache(redis)
    await cache.add(NAMESPACE, KEY, decision("ESCALATE", "new", 0), NOW)
    await cache.fill(NAMESPACE, KEY, [], Facts())  # stale rebuild: saw nothing
    events, facts = await cache.read(NAMESPACE, KEY, NOW)
    assert [e.ref_id for e in events] == ["new"]
    assert facts.seen is True


async def test_adding_the_same_event_twice_counts_once(redis):
    cache = MemoryCache(redis)
    await cache.fill(NAMESPACE, KEY, [], Facts())
    for _ in range(2):
        await cache.add(NAMESPACE, KEY, decision("ESCALATE", "a", 1), NOW)
    events, _ = await cache.read(NAMESPACE, KEY, NOW)
    assert len(events) == 1


# --------------------------------------------------------------- service


async def test_miss_falls_back_to_postgres_and_repopulates(service, store, redis):
    await store.insert(APP_ID, KEY, decision("ESCALATE", "a", 0.5))
    await store.insert(APP_ID, KEY, decision("SUPPRESS", "b", 5))

    first = await service.get_context(APP_ID, KEY)
    assert store.loads == 1
    assert (first.window_1h.alert_count, first.window_24h.alert_count) == (1, 2)
    assert first.is_novel_alert is False
    assert await redis.hget(f"mem:{NAMESPACE}:{KEY}:facts", "loaded") == "1"

    second = await service.get_context(APP_ID, KEY)
    assert store.loads == 1  # served from Redis
    assert second == first


async def test_novel_alert_is_cached_too(service, store):
    ctx = await service.get_context(APP_ID, "never:seen")
    assert ctx.is_novel_alert is True
    await service.get_context(APP_ID, "never:seen")
    assert store.loads == 1


async def test_record_updates_a_loaded_key_without_a_rebuild(service, store):
    await service.get_context(APP_ID, KEY)
    assert await service.record(APP_ID, KEY, decision("SUPPRESS", "a", 0.1)) is True
    ctx = await service.get_context(APP_ID, KEY)
    assert store.loads == 1
    assert ctx.window_1h.suppression_count == 1
    assert ctx.is_novel_alert is False


async def test_duplicate_event_is_counted_once(service):
    assert await service.record(APP_ID, KEY, decision("ESCALATE", "a", 1)) is True
    assert await service.record(APP_ID, KEY, decision("ESCALATE", "a", 1)) is False
    assert (await service.get_context(APP_ID, KEY)).window_24h.escalation_count == 1


async def test_windows_slide_as_time_passes(service, clock):
    await service.record(APP_ID, KEY, decision("ESCALATE", "a", 0.5))
    assert (await service.get_context(APP_ID, KEY)).window_1h.alert_count == 1
    clock.now += H
    ctx = await service.get_context(APP_ID, KEY)
    assert (ctx.window_1h.alert_count, ctx.window_24h.alert_count) == (0, 1)


async def test_confirmed_incident_fact_from_postgres(service, store):
    await store.insert(APP_ID, KEY, Event(CONFIRMED_INCIDENT, "case-1", NOW - 30 * 24 * H))
    assert (await service.get_context(APP_ID, KEY)).has_confirmed_incident_history is True


async def test_same_alert_key_in_two_apps_never_collides(store, redis, clock):
    from app.core.service import MemoryService
    from tests.conftest import FakeApps

    service = MemoryService(store, MemoryCache(redis), FakeApps({"app-a": "ns-a", "app-b": "ns-b"}), clock=clock)
    await service.record("app-a", KEY, decision("ESCALATE", "x", 1))
    assert (await service.get_context("app-a", KEY)).window_24h.alert_count == 1
    assert (await service.get_context("app-b", KEY)).window_24h.alert_count == 0


async def test_unknown_app_raises_for_get_context(service):
    with pytest.raises(AppNotFoundError):
        await service.get_context("no-such-app", KEY)


async def test_unknown_app_event_is_stored_but_not_cached(service, store, redis):
    assert await service.record("no-such-app", KEY, decision("ESCALATE", "a", 1)) is True
    assert len(store.rows) == 1
    assert await redis.keys("*") == []


async def test_registry_down_propagates_from_record(service, apps, store):
    apps.down = True
    with pytest.raises(RegistryUnavailableError):
        await service.record(APP_ID, KEY, decision("ESCALATE", "a", 1))
    assert store.rows == {}  # nothing half-written; the consumer retries


class BrokenRedis:
    def pipeline(self, *a, **k):
        raise RedisConnectionError("redis down")

    async def delete(self, *keys):
        raise RedisConnectionError("redis down")


async def test_redis_down_answers_from_postgres(store, apps, clock):
    from app.core.service import MemoryService

    service = MemoryService(store, MemoryCache(BrokenRedis()), apps, clock=clock)
    await store.insert(APP_ID, KEY, decision("SUPPRESS", "a", 1))
    assert (await service.get_context(APP_ID, KEY)).window_24h.suppression_count == 1
    # record still stores to Postgres
    assert await service.record(APP_ID, KEY, decision("SUPPRESS", "b", 1)) is True
    assert len(store.rows) == 2
