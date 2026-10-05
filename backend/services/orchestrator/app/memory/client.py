"""gRPC client for `memory-store`'s `GetContext` (docs/adr/0019).

One `grpc.aio` channel per process, opened at startup and reused for every
call; gRPC reconnects it by itself after `memory-store` restarts. Each call
has a deadline, so a hung `memory-store` costs a run at most `timeout_s`.

Any failure (unreachable, deadline, any error status) raises
`ContextUnavailableError`: the run goes on without context, and the
supervisor escalates it (docs/adr/0020).
"""

import logging
from typing import Protocol

import grpc

from proto_gen import memory_store_pb2, memory_store_pb2_grpc

logger = logging.getLogger(__name__)


class ContextUnavailableError(RuntimeError):
    pass


class ContextSource(Protocol):
    """`MemoryStoreClient`, or a fake in tests."""

    async def get_context(self, app_id: str, alert_key: str) -> memory_store_pb2.GetContextResponse: ...


class MemoryStoreClient:
    def __init__(self, target: str, timeout_s: float = 1.0):
        self._target = target
        self._timeout_s = timeout_s
        # Plaintext inside the platform network (§13 T12).
        self._channel = grpc.aio.insecure_channel(target)
        self._stub = memory_store_pb2_grpc.MemoryStoreStub(self._channel)

    async def get_context(self, app_id: str, alert_key: str) -> memory_store_pb2.GetContextResponse:
        request = memory_store_pb2.GetContextRequest(app_id=app_id, alert_key=alert_key)
        try:
            return await self._stub.GetContext(request, timeout=self._timeout_s)
        except grpc.aio.AioRpcError as e:
            logger.warning(
                "GetContext failed for app_id=%s alert_key=%s: %s %s",
                app_id, alert_key, e.code().name, e.details(),
            )
            raise ContextUnavailableError(f"{e.code().name}: {e.details()}") from None

    async def aclose(self) -> None:
        await self._channel.close()
