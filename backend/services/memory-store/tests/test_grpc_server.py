"""GetContext over a real gRPC channel, against fakes."""

import socket

import grpc
import pytest

from app.grpc.server import start_grpc_server
from proto_gen import memory_store_pb2, memory_store_pb2_grpc
from tests.conftest import APP_ID, KEY, decision

pytestmark = pytest.mark.anyio


@pytest.fixture
def unused_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


@pytest.fixture
async def stub(service, unused_port):
    server = await start_grpc_server(service, unused_port)
    async with grpc.aio.insecure_channel(f"localhost:{unused_port}") as channel:
        yield memory_store_pb2_grpc.MemoryStoreStub(channel)
    await server.stop(grace=None)


async def test_get_context_returns_counts(stub, service):
    await service.record(APP_ID, KEY, decision("SUPPRESS", "a", 2))
    response = await stub.GetContext(memory_store_pb2.GetContextRequest(app_id=APP_ID, alert_key=KEY))
    assert (response.app_id, response.alert_key) == (APP_ID, KEY)
    assert (response.window_1h.alert_count, response.window_24h.suppression_count) == (0, 1)
    assert response.is_novel_alert is False


@pytest.mark.parametrize(
    "request_fields, code",
    [
        ({"app_id": APP_ID}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"alert_key": KEY}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"app_id": "no-such-app", "alert_key": KEY}, grpc.StatusCode.NOT_FOUND),
    ],
)
async def test_bad_requests_get_clear_status_codes(stub, request_fields, code):
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await stub.GetContext(memory_store_pb2.GetContextRequest(**request_fields))
    assert e.value.code() == code


async def test_registry_down_is_unavailable(stub, apps):
    apps.down = True
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await stub.GetContext(memory_store_pb2.GetContextRequest(app_id=APP_ID, alert_key=KEY))
    assert e.value.code() == grpc.StatusCode.UNAVAILABLE
