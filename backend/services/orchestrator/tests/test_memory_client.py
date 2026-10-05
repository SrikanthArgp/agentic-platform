"""`MemoryStoreClient` against a real gRPC server with a fake `MemoryStore` servicer."""

import asyncio

import grpc
import pytest

from app.memory.client import ContextUnavailableError, MemoryStoreClient
from proto_gen import memory_store_pb2, memory_store_pb2_grpc

pytestmark = pytest.mark.anyio


class FakeMemoryStore(memory_store_pb2_grpc.MemoryStoreServicer):
    def __init__(self):
        self.peers: list[str] = []
        self.status: grpc.StatusCode | None = None
        self.delay_s = 0.0

    async def GetContext(self, request, context):
        self.peers.append(context.peer())
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.status:
            await context.abort(self.status, "nope")
        return memory_store_pb2.GetContextResponse(
            app_id=request.app_id, alert_key=request.alert_key, is_novel_alert=True
        )


@pytest.fixture
async def memory_store(unused_port):
    servicer = FakeMemoryStore()
    server = grpc.aio.server()
    memory_store_pb2_grpc.add_MemoryStoreServicer_to_server(servicer, server)
    server.add_insecure_port(f"localhost:{unused_port}")
    await server.start()
    yield servicer
    await server.stop(grace=None)


@pytest.fixture
async def client(memory_store, unused_port):
    c = MemoryStoreClient(f"localhost:{unused_port}", timeout_s=0.5)
    yield c
    await c.aclose()


async def test_returns_the_context_for_app_and_key(client):
    ctx = await client.get_context("it-ops-triage", "disk_full:web-01")
    assert (ctx.app_id, ctx.alert_key, ctx.is_novel_alert) == ("it-ops-triage", "disk_full:web-01", True)


async def test_reuses_one_connection_across_calls(client, memory_store):
    for _ in range(5):
        await client.get_context("it-ops-triage", "disk_full:web-01")
    # Same client address and port on every call: one channel, one connection.
    assert len(set(memory_store.peers)) == 1


@pytest.mark.parametrize(
    "status", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT]
)
async def test_any_error_status_is_context_unavailable(client, memory_store, status):
    memory_store.status = status
    with pytest.raises(ContextUnavailableError, match=status.name):
        await client.get_context("it-ops-triage", "disk_full:web-01")


async def test_slow_memory_store_hits_the_deadline(client, memory_store):
    memory_store.delay_s = 2
    with pytest.raises(ContextUnavailableError, match="DEADLINE_EXCEEDED"):
        await client.get_context("it-ops-triage", "disk_full:web-01")


async def test_unreachable_memory_store_is_context_unavailable(unused_port):
    client = MemoryStoreClient(f"localhost:{unused_port}", timeout_s=0.5)  # nothing listening
    try:
        with pytest.raises(ContextUnavailableError):
            await client.get_context("it-ops-triage", "disk_full:web-01")
    finally:
        await client.aclose()
