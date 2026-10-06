"""`alert.received` message handling (the pure part under the Kafka loop)."""

from contextlib import asynccontextmanager

import pytest

from app.kafka.alerts import handle_alert, message_key
from app.tools.gateway import GatewayUnavailableError
from proto_gen import agent_pb2
from registry_client import RegistryUnavailableError
from tests.conftest import FakeChatModel, FakeGateway, FakeMemory, final, make_request, make_runner

pytestmark = pytest.mark.anyio


class DownGateway(FakeGateway):
    @asynccontextmanager
    async def connect(self):
        raise GatewayUnavailableError("tool-gateway unreachable (ConnectError: connection refused)")
        yield


class BrokenMemory(FakeMemory):
    """A bug, not an outage: anything but ContextUnavailableError."""

    async def get_context(self, app_id, alert_key):
        raise RuntimeError("bug")


def test_message_key_is_app_id_and_alert_key():
    assert message_key("it-ops-triage", "disk_full:web-01") == b"it-ops-triage:disk_full:web-01"


async def test_decodes_runs_and_returns_key_and_decision(apps_dir):
    request = make_request()
    key, response = await handle_alert(request.SerializeToString(), make_runner(apps_dir, FakeChatModel(responses=[final()])))

    assert key == b"test-app:disk_full:web-01"
    assert response.alert_id == "alert-1"
    assert response.decision == agent_pb2.AUTO_RESOLVE
    # Round-trips as the alert.decided payload.
    assert agent_pb2.RunAgentResponse.FromString(response.SerializeToString()) == response


async def test_llm_down_still_publishes_escalate(apps_dir):
    runner = make_runner(apps_dir, FakeChatModel(responses=[TimeoutError("read timed out")]))
    _, response = await handle_alert(make_request().SerializeToString(), runner)

    assert response.decision == agent_pb2.ESCALATE
    assert "not evaluated: LLM unavailable (TimeoutError: read timed out)" in response.reasons[0]


async def test_any_other_failure_still_publishes_escalate(apps_dir):
    runner = make_runner(apps_dir, FakeChatModel(responses=[]), memory=BrokenMemory())
    _, response = await handle_alert(make_request().SerializeToString(), runner)

    assert response.decision == agent_pb2.ESCALATE
    assert "not evaluated: agent run failed" in response.reasons[0]


async def test_unknown_app_publishes_escalate(apps_dir):
    request = make_request()
    request.app_id = "no-such-app"
    _, response = await handle_alert(request.SerializeToString(), make_runner(apps_dir, FakeChatModel(responses=[])))

    assert response.decision == agent_pb2.ESCALATE
    assert "No app 'no-such-app' is registered" in response.reasons[0]


async def test_registry_down_publishes_escalate(apps_dir):
    class Down:
        async def get_app(self, app_id):
            raise RegistryUnavailableError("connection refused")

    runner = make_runner(apps_dir, FakeChatModel(responses=[]), registry=Down())
    _, response = await handle_alert(make_request().SerializeToString(), runner)

    assert response.decision == agent_pb2.ESCALATE
    assert "not evaluated: registry unavailable" in response.reasons[0]


async def test_tool_gateway_down_publishes_escalate(apps_dir):
    runner = make_runner(apps_dir, FakeChatModel(responses=[]), gateway=DownGateway())
    _, response = await handle_alert(make_request().SerializeToString(), runner)

    assert response.decision == agent_pb2.ESCALATE
    assert "not evaluated: tool-gateway unreachable (ConnectError: connection refused)" in response.reasons[0]


@pytest.mark.parametrize("raw", [b"\xff\xff\xff", agent_pb2.RunAgentRequest(app_id="a").SerializeToString()])
async def test_undecodable_or_unidentifiable_message_is_skipped(apps_dir, raw):
    assert await handle_alert(raw, make_runner(apps_dir, FakeChatModel(responses=[]))) is None
