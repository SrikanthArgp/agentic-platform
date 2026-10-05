"""`RunAgent` over a real gRPC channel, against the fake LLM and tool-gateway."""

import grpc
import pytest

from app.grpc.server import start_grpc_server
from proto_gen import agent_pb2, agent_pb2_grpc
from tests.conftest import FakeLLM, final, make_request, make_runner

pytestmark = pytest.mark.anyio


@pytest.fixture
async def stub(apps_dir, unused_port):
    server = await start_grpc_server(make_runner(apps_dir, FakeLLM([final("SUPPRESS", "flap")])), unused_port)
    async with grpc.aio.insecure_channel(f"localhost:{unused_port}") as channel:
        yield agent_pb2_grpc.AgentStub(channel)
    await server.stop(grace=None)


async def test_run_agent_returns_decision(stub):
    response = await stub.RunAgent(make_request())
    assert response.decision == agent_pb2.SUPPRESS
    assert list(response.reasons) == ["flap"]


async def test_unknown_app_is_not_found(stub):
    request = make_request()
    request.app_id = "no-such-app"
    with pytest.raises(grpc.aio.AioRpcError) as e:
        await stub.RunAgent(request)
    assert e.value.code() == grpc.StatusCode.NOT_FOUND
