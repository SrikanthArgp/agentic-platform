"""review-console against the running Compose stack.

Publishes `alert.decided` straight to Kafka (no LLM involved), then drives
the REST API on :8006 and reads `verdict.recorded` back. Each test uses its
own random alert_key, so it can run against a stack with real history. Run
with `uv run pytest -m integration`.
"""

import asyncio
import json
import os
import uuid

import httpx
import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from proto_gen import agent_pb2

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

APP_ID = "it-ops-triage"
BASE_URL = os.environ.get("REVIEW_CONSOLE_URL", "http://localhost:8006")
KAFKA = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def alert_key() -> str:
    return f"it_test:{uuid.uuid4().hex[:12]}"


async def publish_decided(alert_key: str, alert_id: str, decision: int) -> None:
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA, enable_idempotence=True, acks="all")
    await producer.start()
    try:
        response = agent_pb2.RunAgentResponse(
            app_id=APP_ID, agent_id="triage-agent", alert_id=alert_id, alert_key=alert_key, decision=decision,
            reasons=["integration test"], confidence=0.42,
            tool_calls=[agent_pb2.ToolCall(tool_name="lookup_runbook", result_summary="none", agent_id="triage-agent")],
            alert=agent_pb2.RunAgentRequest(
                app_id=APP_ID, alert_id=alert_id, alert_key=alert_key, source="it-test", severity="critical",
                message="integration test alert", timestamp_unix_ms=1_791_000_000_000,
            ),
        )
        await producer.send_and_wait("alert.decided", response.SerializeToString(), key=f"{APP_ID}:{alert_key}".encode())
    finally:
        await producer.stop()


async def wait_for_cases(client: httpx.AsyncClient, alert_key: str, n: int, timeout_s: float = 15) -> list[dict]:
    for _ in range(int(timeout_s / 0.25)):
        cases = (await client.get("/cases", params={"app_id": APP_ID, "alert_key": alert_key})).json()["cases"]
        if len(cases) >= n:
            return cases
        await asyncio.sleep(0.25)
    raise AssertionError(f"expected {n} case(s) for {alert_key}, got {len(cases)}")


async def read_verdict(alert_key: str) -> tuple[bytes, dict]:
    """This test's verdict.recorded event: the topic is read from the start by
    a throwaway group, skipping other tests' and real verdicts."""
    consumer = AIOKafkaConsumer(
        "verdict.recorded", bootstrap_servers=KAFKA, group_id=f"it-test-{uuid.uuid4().hex[:8]}",
        auto_offset_reset="earliest", enable_auto_commit=False,
    )
    await consumer.start()
    try:
        while True:
            msg = await asyncio.wait_for(consumer.getone(), timeout=15)
            event = json.loads(msg.value)
            if event["alert_key"] == alert_key:
                return msg.key, event
    finally:
        await consumer.stop()


async def test_escalation_becomes_a_case_and_a_verdict_is_published(alert_key):
    await publish_decided(alert_key, f"{alert_key}-esc", agent_pb2.ESCALATE)
    await publish_decided(alert_key, f"{alert_key}-sup", agent_pb2.SUPPRESS)
    await publish_decided(alert_key, f"{alert_key}-esc", agent_pb2.ESCALATE)  # redelivery

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5) as client:
        [case] = await wait_for_cases(client, alert_key, 1)
        await asyncio.sleep(1)  # let the SUPPRESS and the duplicate land too
        assert len(await wait_for_cases(client, alert_key, 1)) == 1
        assert (case["alert_id"], case["status"], case["confidence"]) == (f"{alert_key}-esc", "OPEN", 0.42)
        assert case["tool_calls"] == [{"tool_name": "lookup_runbook", "result_summary": "none", "agent_id": "triage-agent"}]
        assert (case["alert"]["severity"], case["alert"]["message"]) == ("critical", "integration test alert")

        r = await client.post(
            f"/cases/{case['id']}/verdict",
            json={"verdict": "CONFIRMED_NOISE", "verdict_by": "it-test", "resolution_notes": "test only"},
        )
        assert r.status_code == 200, r.text
        again = await client.post(f"/cases/{case['id']}/verdict", json={"verdict": "CONFIRMED_INCIDENT", "verdict_by": "x"})
        assert again.status_code == 409

    key, event = await read_verdict(alert_key)
    assert key == f"{APP_ID}:{alert_key}".encode()
    assert event["case_id"] == case["id"] and event["verdict"] == "CONFIRMED_NOISE"
    assert "resolution_notes" not in event
