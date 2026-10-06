"""handle_verdict: verdict.recorded JSON -> one verdict event (plan.md Day 10)."""

import json

import pytest

from app.core.events import verdict_event
from app.kafka.verdicts import handle_verdict
from tests.conftest import APP_ID, H, KEY, NOW, FakeApps, decision

pytestmark = pytest.mark.anyio

OTHER_APP = "other-app"


def verdict(**overrides) -> bytes:
    body = {
        "app_id": APP_ID, "case_id": 7, "alert_key": KEY, "verdict": "CONFIRMED_NOISE",
        "verdict_by": "ana", "recorded_at": "2026-10-06T12:00:00+00:00",
    }
    body.update(overrides)
    return json.dumps(body).encode()


async def test_noise_verdict_raises_confirmed_noise_in_every_window(service, store):
    await store.insert(APP_ID, KEY, decision("ESCALATE", "al-1", hours_ago=2))
    before = await service.get_context(APP_ID, KEY)
    assert await handle_verdict(verdict(), NOW - 1000, service) is True

    ctx = await service.get_context(APP_ID, KEY)
    for window in (ctx.window_1h, ctx.window_24h, ctx.window_7d):
        assert (window.confirmed_noise_count, window.confirmed_incident_count) == (1, 0)
    assert ctx.has_confirmed_incident_history is False
    # A verdict is not a decision: alert counts and novelty are unchanged.
    assert ctx.window_24h.alert_count == before.window_24h.alert_count == 1
    assert ctx.is_novel_alert is False


async def test_incident_verdict_raises_confirmed_incident_and_sets_the_flag(service):
    assert await handle_verdict(verdict(verdict="CONFIRMED_INCIDENT"), NOW, service) is True
    ctx = await service.get_context(APP_ID, KEY)
    for window in (ctx.window_1h, ctx.window_24h, ctx.window_7d):
        assert (window.confirmed_incident_count, window.confirmed_noise_count) == (1, 0)
    assert ctx.has_confirmed_incident_history is True


async def test_the_incident_flag_outlives_the_7d_window(service, clock):
    await handle_verdict(verdict(verdict="CONFIRMED_INCIDENT"), NOW, service)
    clock.now = NOW + 8 * 24 * H
    ctx = await service.get_context(APP_ID, KEY)
    assert ctx.window_7d.confirmed_incident_count == 0
    assert ctx.has_confirmed_incident_history is True


async def test_verdict_updates_an_already_cached_key(service, store):
    await service.get_context(APP_ID, KEY)  # cache the key first
    loads = store.loads
    await handle_verdict(verdict(), NOW, service)
    await handle_verdict(verdict(case_id=8, verdict="CONFIRMED_INCIDENT"), NOW, service)
    ctx = await service.get_context(APP_ID, KEY)
    assert store.loads == loads  # answered from Redis, not rebuilt
    assert (ctx.window_1h.confirmed_noise_count, ctx.window_1h.confirmed_incident_count) == (1, 1)
    assert ctx.has_confirmed_incident_history is True


async def test_a_verdict_for_app_a_never_touches_app_b(store, redis, clock):
    from app.core.service import MemoryService
    from app.redis.cache import MemoryCache

    service = MemoryService(store, MemoryCache(redis), FakeApps({APP_ID: "ns-a", OTHER_APP: "ns-b"}), clock=clock)
    await handle_verdict(verdict(verdict="CONFIRMED_INCIDENT"), NOW, service)

    other = await service.get_context(OTHER_APP, KEY)  # same alert_key, other app
    assert other.window_7d.confirmed_incident_count == 0
    assert other.has_confirmed_incident_history is False
    assert (await service.get_context(APP_ID, KEY)).has_confirmed_incident_history is True


async def test_redelivered_verdict_counts_once(service):
    for _ in range(3):
        await handle_verdict(verdict(), NOW, service)
    assert (await service.get_context(APP_ID, KEY)).window_1h.confirmed_noise_count == 1


async def test_verdicts_on_two_cases_count_twice(service):
    await handle_verdict(verdict(case_id=1), NOW, service)
    await handle_verdict(verdict(case_id=2), NOW, service)
    assert (await service.get_context(APP_ID, KEY)).window_1h.confirmed_noise_count == 2


async def test_a_verdict_and_a_decision_with_the_same_ref_are_both_kept(service, store):
    """Unique per (app_id, family, ref_id): a case id equal to an alert id can't collide."""
    await store.insert(APP_ID, KEY, decision("ESCALATE", "7", hours_ago=0.1))
    assert await handle_verdict(verdict(case_id=7), NOW, service) is True
    ctx = await service.get_context(APP_ID, KEY)
    assert (ctx.window_1h.escalation_count, ctx.window_1h.confirmed_noise_count) == (1, 1)


async def test_the_event_is_timed_by_the_message_and_keyed_by_case(service, store):
    await handle_verdict(verdict(case_id=42), NOW - 5000, service)
    [(alert_key, event)] = store.rows.values()
    assert alert_key == KEY
    assert (event.kind, event.ref_id, event.at_ms) == ("verdict:CONFIRMED_NOISE", "42", NOW - 5000)


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe",
        b"not json",
        b"[1, 2]",
        verdict(app_id=""),
        verdict(alert_key=None),
        verdict(case_id=None),
        verdict(case_id=True),
        verdict(verdict="MAYBE"),
        verdict(verdict=None),
    ],
    ids=["bytes", "not-json", "not-object", "no-app_id", "no-alert_key", "no-case_id", "bool-case_id",
         "unknown-verdict", "no-verdict"],
)
async def test_unusable_message_is_skipped(service, store, raw):
    assert await handle_verdict(raw, NOW, service) is False
    assert store.rows == {}


async def test_registry_down_raises_so_the_consumer_retries(service, apps, store):
    from registry_client import RegistryUnavailableError

    apps.down = True
    with pytest.raises(RegistryUnavailableError):
        await handle_verdict(verdict(), NOW, service)
    assert store.rows == {}


def test_verdict_event_rejects_unknown_verdicts():
    assert verdict_event("1", "CONFIRMED_INCIDENT", NOW).kind == "verdict:CONFIRMED_INCIDENT"
    with pytest.raises(ValueError):
        verdict_event("1", "NOISE", NOW)


def test_the_consumer_reads_both_topics_in_one_group():
    from app.kafka.consumer import CONSUMER_GROUP, HANDLERS
    from app.kafka.decisions import handle_decided

    assert dict(HANDLERS) == {"alert.decided": handle_decided, "verdict.recorded": handle_verdict}
    assert CONSUMER_GROUP == "memory-store"
