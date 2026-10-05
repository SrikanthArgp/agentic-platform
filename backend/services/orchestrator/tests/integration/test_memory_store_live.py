"""`MemoryStoreClient` and a whole run against the real Compose `memory-store`
(localhost:50053), with the fake LLM and tool-gateway.

Needs `backend/local` up, `it-ops-triage` registered, and
`uv run backend/scripts/seed.py` run (the seeded `healthcheck_flap:lb-02`).
"""

import os
import uuid

import pytest

from app.memory.client import MemoryStoreClient
from proto_gen import agent_pb2
from tests.conftest import FakeChatModel, FakeRegistry, final, make_request, make_runner

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TARGET = os.environ.get("MEMORY_STORE_TARGET", "localhost:50053")
APP_ID = "it-ops-triage"


@pytest.fixture
async def memory():
    client = MemoryStoreClient(TARGET, timeout_s=2)
    yield client
    await client.aclose()


async def test_seeded_key_has_history_and_unseen_key_is_novel(memory):
    seeded = await memory.get_context(APP_ID, "healthcheck_flap:lb-02")
    assert not seeded.is_novel_alert
    assert seeded.window_7d.suppression_count >= 17

    unseen = await memory.get_context(APP_ID, f"healthcheck_flap:it-{uuid.uuid4().hex[:8]}")
    assert unseen.is_novel_alert
    assert unseen.window_7d.alert_count == 0


async def test_same_agent_answer_decides_differently_on_real_history(apps_dir, memory):
    """Day 7 DoD against real memory: the agent says SUPPRESS both times."""
    registry = FakeRegistry()
    registry.apps[APP_ID] = {**registry.apps.pop("test-app"), "app_id": APP_ID, "memory_namespace": APP_ID}
    (apps_dir / "test-app").rename(apps_dir / APP_ID)

    decisions = {}
    for host in ("lb-02", f"it-{uuid.uuid4().hex[:8]}"):
        request = make_request(alert_type="healthcheck_flap", host=host)
        request.app_id, request.alert_key = APP_ID, f"healthcheck_flap:{host}"
        runner = make_runner(apps_dir, FakeChatModel(responses=[final("SUPPRESS", "RB-006: flap")]), registry=registry, memory=memory)
        decisions[host == "lb-02"] = (await runner.run(request)).decision

    assert decisions == {True: agent_pb2.SUPPRESS, False: agent_pb2.ESCALATE}
