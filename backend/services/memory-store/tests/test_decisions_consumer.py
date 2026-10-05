"""handle_decided: alert.decided bytes -> one decision event."""

import pytest

from app.kafka.decisions import handle_decided
from proto_gen import agent_pb2
from tests.conftest import APP_ID, KEY, NOW

pytestmark = pytest.mark.anyio


def decided(**overrides) -> bytes:
    fields = dict(app_id=APP_ID, agent_id="triage-agent", alert_id="al-1", alert_key=KEY, decision=agent_pb2.ESCALATE)
    fields.update(overrides)
    return agent_pb2.RunAgentResponse(**fields).SerializeToString()


async def test_decision_is_recorded_at_the_message_time(service, store):
    assert await handle_decided(decided(), NOW - 1000, service) is True
    [(alert_key, event)] = store.rows.values()
    assert alert_key == KEY
    assert (event.kind, event.ref_id, event.at_ms) == ("decision:ESCALATE", "al-1", NOW - 1000)
    ctx = await service.get_context(APP_ID, KEY)
    assert (ctx.window_1h.alert_count, ctx.window_1h.escalation_count) == (1, 1)


async def test_redelivered_message_counts_once(service):
    for _ in range(3):
        await handle_decided(decided(), NOW, service)
    assert (await service.get_context(APP_ID, KEY)).window_1h.alert_count == 1


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xff\xff", decided(alert_key=""), decided(app_id=""), decided(alert_id="")],
    ids=["undecodable", "no-alert_key", "no-app_id", "no-alert_id"],
)
async def test_unusable_message_is_skipped(service, store, raw):
    assert await handle_decided(raw, NOW, service) is False
    assert store.rows == {}


async def test_registry_down_raises_so_the_consumer_retries(service, apps, store):
    from registry_client import RegistryUnavailableError

    apps.down = True
    with pytest.raises(RegistryUnavailableError):
        await handle_decided(decided(), NOW, service)
    assert store.rows == {}
