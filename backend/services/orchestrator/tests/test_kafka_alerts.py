"""`alert.received` message handling (the pure part under the Kafka loop)."""

import pytest

from app.agent.llm import LLMError
from app.kafka.alerts import handle_alert, message_key
from proto_gen import agent_pb2
from tests.conftest import FakeLLM, final, make_request, make_runner

pytestmark = pytest.mark.anyio


class FailingLLM:
    model = "down"

    def __init__(self, error: Exception):
        self._error = error

    async def complete(self, **_):
        raise self._error


def test_message_key_is_app_id_and_alert_key():
    assert message_key("it-ops-triage", "disk_full:web-01") == b"it-ops-triage:disk_full:web-01"


async def test_decodes_runs_and_returns_key_and_decision(apps_dir):
    request = make_request()
    key, response = await handle_alert(request.SerializeToString(), make_runner(apps_dir, FakeLLM([final()])))

    assert key == b"test-app:disk_full:web-01"
    assert response.alert_id == "alert-1"
    assert response.decision == agent_pb2.AUTO_RESOLVE
    # Round-trips as the alert.decided payload.
    assert agent_pb2.RunAgentResponse.FromString(response.SerializeToString()) == response


@pytest.mark.parametrize(
    "error, why", [(LLMError("timeout"), "LLM unavailable"), (RuntimeError("bug"), "agent run failed")]
)
async def test_failed_run_still_publishes_escalate(apps_dir, error, why):
    _, response = await handle_alert(make_request().SerializeToString(), make_runner(apps_dir, FailingLLM(error)))

    assert response.decision == agent_pb2.ESCALATE
    assert why in response.reasons[0]


async def test_unknown_app_publishes_escalate(apps_dir):
    request = make_request()
    request.app_id = "no-such-app"
    _, response = await handle_alert(request.SerializeToString(), make_runner(apps_dir, FakeLLM([])))

    assert response.decision == agent_pb2.ESCALATE
    assert "No manifest for app_id 'no-such-app'" in response.reasons[0]


@pytest.mark.parametrize("raw", [b"\xff\xff\xff", agent_pb2.RunAgentRequest(app_id="a").SerializeToString()])
async def test_undecodable_or_unidentifiable_message_is_skipped(apps_dir, raw):
    assert await handle_alert(raw, make_runner(apps_dir, FakeLLM([]))) is None
