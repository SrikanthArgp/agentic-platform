"""handle_decided: alert.decided bytes -> a case, or nothing."""

import pytest

from app.kafka.decisions import handle_decided
from proto_gen import agent_pb2
from tests.conftest import decided

pytestmark = pytest.mark.anyio


async def test_escalation_message_becomes_a_case(service, store):
    assert await handle_decided(decided().SerializeToString(), service) is True
    assert [c.alert_id for c in store.cases.values()] == ["al-1"]


async def test_suppress_message_is_handled_without_a_case(service, store):
    assert await handle_decided(decided(decision=agent_pb2.SUPPRESS).SerializeToString(), service) is True
    assert store.cases == {}


async def test_redelivered_message_creates_one_case(service, store):
    for _ in range(3):
        await handle_decided(decided().SerializeToString(), service)
    assert len(store.cases) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xff\xff",
        decided(alert_key="").SerializeToString(),
        decided(app_id="").SerializeToString(),
        decided(alert_id="").SerializeToString(),
    ],
    ids=["undecodable", "no-alert_key", "no-app_id", "no-alert_id"],
)
async def test_unusable_message_is_skipped(service, store, raw):
    assert await handle_decided(raw, service) is False
    assert store.cases == {}
