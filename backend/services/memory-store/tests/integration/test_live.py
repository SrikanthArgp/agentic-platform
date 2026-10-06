"""memory-store against the running Compose stack (it-ops-triage registered).

Each test uses its own random alert_key, so it can run against a stack with
real history. Run with `uv run pytest -m integration`.
"""

import asyncio
import json
import os
import time
import uuid

import grpc
import pytest
from aiokafka import AIOKafkaProducer

from app.core.config import Settings
from app.core.events import Event, Facts
from app.db.postgres import PostgresEventStore, create_pool
from proto_gen import agent_pb2, memory_store_pb2, memory_store_pb2_grpc

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

APP_ID = "it-ops-triage"
GRPC_TARGET = os.environ.get("MEMORY_STORE_GRPC", "localhost:50053")
H = 3_600_000


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def store():
    pool = await create_pool(Settings.from_env().postgres_dsn)
    yield PostgresEventStore(pool)
    await pool.close()


@pytest.fixture
def alert_key() -> str:
    return f"it_test:{uuid.uuid4().hex[:12]}"


async def get_context(alert_key: str) -> memory_store_pb2.GetContextResponse:
    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = memory_store_pb2_grpc.MemoryStoreStub(channel)
        return await stub.GetContext(memory_store_pb2.GetContextRequest(app_id=APP_ID, alert_key=alert_key))


async def test_postgres_store_insert_is_idempotent_and_load_filters_by_time(store, alert_key):
    now = int(time.time() * 1000)
    new = Event("decision:SUPPRESS", f"{alert_key}-1", now - H)
    old = Event("decision:ESCALATE", f"{alert_key}-2", now - 10 * 24 * H)
    assert await store.insert(APP_ID, alert_key, new) is True
    assert await store.insert(APP_ID, alert_key, new) is False
    await store.insert(APP_ID, alert_key, old)

    events, facts = await store.load(APP_ID, alert_key, now - 7 * 24 * H)
    assert events == [new]
    assert facts == Facts(seen=True)


async def test_grpc_get_context_from_seeded_postgres(store, alert_key):
    now = int(time.time() * 1000)
    for i, (kind, hours) in enumerate([("ESCALATE", 0.2), ("SUPPRESS", 3), ("SUPPRESS", 50)]):
        await store.insert(APP_ID, alert_key, Event(f"decision:{kind}", f"{alert_key}-{i}", int(now - hours * H)))

    ctx = await get_context(alert_key)
    assert (ctx.window_1h.alert_count, ctx.window_24h.alert_count, ctx.window_7d.alert_count) == (1, 2, 3)
    assert (ctx.window_7d.escalation_count, ctx.window_7d.suppression_count) == (1, 2)
    assert ctx.is_novel_alert is False
    assert await get_context(alert_key) == ctx  # second call: from Redis


async def test_novel_key_and_unknown_app():
    assert (await get_context(f"it_test:{uuid.uuid4().hex}")).is_novel_alert is True
    async with grpc.aio.insecure_channel(GRPC_TARGET) as channel:
        stub = memory_store_pb2_grpc.MemoryStoreStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as e:
            await stub.GetContext(memory_store_pb2.GetContextRequest(app_id="made-up-app", alert_key="x"))
    assert e.value.code() == grpc.StatusCode.NOT_FOUND


async def test_published_decision_is_counted(alert_key):
    await get_context(alert_key)  # load the key first: the consumer must update a cached key
    producer = AIOKafkaProducer(bootstrap_servers=Settings.from_env().kafka_bootstrap_servers)
    await producer.start()
    try:
        response = agent_pb2.RunAgentResponse(
            app_id=APP_ID, agent_id="triage-agent", alert_id=f"{alert_key}-decided",
            alert_key=alert_key, decision=agent_pb2.ESCALATE, reasons=["integration test"],
        )
        await producer.send_and_wait("alert.decided", response.SerializeToString(), key=f"{APP_ID}:{alert_key}".encode())
    finally:
        await producer.stop()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ctx = await get_context(alert_key)
        if ctx.window_1h.escalation_count == 1:
            assert ctx.is_novel_alert is False
            return
        await asyncio.sleep(0.3)
    pytest.fail("decision not counted within 15s")


async def test_published_verdict_changes_the_context(alert_key):
    """Day 10's definition of done at the Kafka level: a verdict.recorded
    event moves GetContext's verdict counts (and the all-time flag)."""
    before = await get_context(alert_key)  # loaded and cached first
    assert before.window_7d.confirmed_incident_count == 0 and not before.has_confirmed_incident_history
    producer = AIOKafkaProducer(bootstrap_servers=Settings.from_env().kafka_bootstrap_servers)
    await producer.start()
    try:
        for case_id, verdict in [(f"{alert_key}-1", "CONFIRMED_NOISE"), (f"{alert_key}-2", "CONFIRMED_INCIDENT")]:
            body = {"app_id": APP_ID, "case_id": case_id, "alert_key": alert_key, "verdict": verdict,
                    "verdict_by": "it-test", "recorded_at": "2026-10-06T12:00:00+00:00"}
            await producer.send_and_wait(
                "verdict.recorded", json.dumps(body).encode(), key=f"{APP_ID}:{alert_key}".encode()
            )
    finally:
        await producer.stop()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ctx = await get_context(alert_key)
        if ctx.window_1h.confirmed_incident_count == 1:
            assert (ctx.window_1h.confirmed_noise_count, ctx.window_7d.confirmed_noise_count) == (1, 1)
            assert ctx.has_confirmed_incident_history is True
            assert ctx.window_7d.alert_count == 0  # a verdict is not a decision
            return
        await asyncio.sleep(0.3)
    pytest.fail("verdicts not counted within 15s")
