"""`AlertPipeline` against the real Compose Kafka (localhost:29092), with the
fake LLM and tool-gateway: consume, decide, publish, commit.

Uses its own throwaway topics and consumer group, so the running
orchestrator container never sees these messages. Run with
`uv run pytest -m integration` while `backend/local` is up.
"""

import asyncio
import os
import uuid

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.agent.llm import LLMError
from app.kafka.alerts import AlertPipeline, message_key
from proto_gen import agent_pb2
from tests.conftest import FakeLLM, final, make_request, make_runner

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092")


@pytest.fixture
async def topics():
    suffix = uuid.uuid4().hex[:8]
    received, decided = f"it.alert.received.{suffix}", f"it.alert.decided.{suffix}"
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    await admin.create_topics([NewTopic(t, num_partitions=3, replication_factor=1) for t in (received, decided)])
    try:
        yield received, decided, f"it-orchestrator-{suffix}", admin
    finally:
        await admin.delete_topics([received, decided])
        await admin.close()


async def _publish(topic: str, requests: list[agent_pb2.RunAgentRequest], partition: int | None = None) -> None:
    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP)
    await producer.start()
    try:
        for r in requests:
            await producer.send_and_wait(
                topic, r.SerializeToString(), key=message_key(r.app_id, r.alert_key), partition=partition
            )
    finally:
        await producer.stop()


async def _wait_committed(admin, group: str, topic: str, total: int, timeout_s: float = 10) -> None:
    """Offsets are committed just after each publish; wait for all of them."""
    async with asyncio.timeout(timeout_s):
        while True:
            offsets = await admin.list_consumer_group_offsets(group)
            if sum(meta.offset for tp, meta in offsets.items() if tp.topic == topic) >= total:
                return
            await asyncio.sleep(0.2)


async def _read(topic: str, count: int, timeout_s: float = 20) -> list:
    consumer = AIOKafkaConsumer(topic, bootstrap_servers=BOOTSTRAP, auto_offset_reset="earliest")
    await consumer.start()
    out = []
    try:
        async with asyncio.timeout(timeout_s):
            async for msg in consumer:
                out.append(msg)
                if len(out) == count:
                    return out
    finally:
        await consumer.stop()


def _requests(n: int) -> list[agent_pb2.RunAgentRequest]:
    out = []
    for i in range(n):
        r = make_request()
        r.alert_id = f"alert-{i}"
        out.append(r)
    return out


async def _run_pipeline(pipeline: AlertPipeline, until):
    pipeline.start()
    try:
        return await until()
    finally:
        await pipeline.stop()


async def test_decides_publishes_keyed_in_order_and_commits(apps_dir, topics):
    received, decided, group, admin = topics
    requests = _requests(3)  # same app_id:alert_key -> same partition, in order
    llm = FakeLLM([final("AUTO_RESOLVE", f"reason {i}") for i in range(3)])
    pipeline = AlertPipeline(
        make_runner(apps_dir, llm), BOOTSTRAP, received_topic=received, decided_topic=decided, group_id=group
    )

    async def until():
        messages = await _read(decided, 3)
        await _wait_committed(admin, group, received, 3)
        return messages

    await _publish(received, requests)
    messages = await _run_pipeline(pipeline, until)

    responses = [agent_pb2.RunAgentResponse.FromString(m.value) for m in messages]
    assert [r.alert_id for r in responses] == ["alert-0", "alert-1", "alert-2"]
    assert all(m.key == b"test-app:disk_full:web-01" for m in messages)
    assert [list(r.reasons) for r in responses] == [["reason 0"], ["reason 1"], ["reason 2"]]


class _DownLLM:
    model = "down"

    async def complete(self, **_):
        raise LLMError("connection refused")


async def test_llm_down_still_publishes_escalate(apps_dir, topics):
    received, decided, group, _ = topics
    pipeline = AlertPipeline(
        make_runner(apps_dir, _DownLLM()), BOOTSTRAP, received_topic=received, decided_topic=decided, group_id=group
    )

    await _publish(received, _requests(1))
    [message] = await _run_pipeline(pipeline, lambda: _read(decided, 1))

    response = agent_pb2.RunAgentResponse.FromString(message.value)
    assert response.decision == agent_pb2.ESCALATE
    assert "not evaluated: LLM unavailable" in response.reasons[0]


async def test_undecodable_message_is_skipped_and_committed(apps_dir, topics):
    received, decided, group, admin = topics
    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP)
    await producer.start()
    await producer.send_and_wait(received, b"\xff\xff\xff", key=b"junk", partition=0)
    await producer.stop()
    # Same partition, after the junk: deciding it proves the pipeline moved past.
    await _publish(received, _requests(1), partition=0)
    pipeline = AlertPipeline(
        make_runner(apps_dir, FakeLLM([final()])), BOOTSTRAP,
        received_topic=received, decided_topic=decided, group_id=group,
    )

    async def until():
        messages = await _read(decided, 1)
        await _wait_committed(admin, group, received, 2)
        return messages

    [message] = await _run_pipeline(pipeline, until)
    assert agent_pb2.RunAgentResponse.FromString(message.value).alert_id == "alert-0"
