"""Duplicate `alert.received` deliveries are skipped (Day 16, §10)."""

import pytest
from fakeredis import FakeAsyncRedis

from app.kafka.alerts import handle_alert
from app.kafka.dedupe import SeenAlerts, seen_key
from proto_gen import agent_pb2
from tests.conftest import FakeChatModel, final, make_request, make_runner
from tests.test_budget import down_redis

pytestmark = pytest.mark.anyio


async def test_marked_alert_is_seen_with_a_ttl_and_per_app():
    redis = FakeAsyncRedis(decode_responses=True)
    seen = SeenAlerts(redis, ttl_s=600)
    assert await seen.seen("app-a", "alert-1") is False

    await seen.mark("app-a", "alert-1")
    assert await seen.seen("app-a", "alert-1") is True
    assert await seen.seen("app-b", "alert-1") is False  # alert ids are per app
    assert 0 < await redis.ttl(seen_key("app-a", "alert-1")) <= 600


async def test_duplicate_alert_is_skipped_without_running_the_agent(apps_dir):
    seen = SeenAlerts(FakeAsyncRedis(decode_responses=True))
    llm = FakeChatModel(responses=[final()])
    raw = make_request().SerializeToString()

    first = await handle_alert(raw, make_runner(apps_dir, llm), seen)
    assert first is not None and first[1].decision == agent_pb2.AUTO_RESOLVE
    await seen.mark(first[1].app_id, first[1].alert_id)  # what the pipeline does after publishing

    calls = len(llm.calls)
    assert calls > 0
    assert await handle_alert(raw, make_runner(apps_dir, llm), seen) is None
    assert len(llm.calls) == calls  # no model call for the duplicate


async def test_unmarked_alert_runs_again_so_a_crash_mid_run_loses_nothing(apps_dir):
    """The marker is set after publishing; a redelivery before that runs."""
    seen = SeenAlerts(FakeAsyncRedis(decode_responses=True))
    raw = make_request().SerializeToString()
    llm = FakeChatModel(responses=[final(), final()])
    assert await handle_alert(raw, make_runner(apps_dir, llm), seen) is not None
    assert await handle_alert(raw, make_runner(apps_dir, llm), seen) is not None


async def test_redis_down_runs_the_alert_and_mark_doesnt_raise(apps_dir):
    seen = SeenAlerts(down_redis())
    assert await seen.seen("test-app", "alert-1") is False
    await seen.mark("test-app", "alert-1")
    llm = FakeChatModel(responses=[final()])
    assert await handle_alert(make_request().SerializeToString(), make_runner(apps_dir, llm), seen) is not None
