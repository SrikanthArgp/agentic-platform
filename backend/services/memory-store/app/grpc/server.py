"""The `MemoryStore` gRPC service: `GetContext` (memory_store.proto, ARCHITECTURE §6).

Status codes, so `orchestrator` (Day 7) can tell its own mistakes from
ours: missing `app_id`/`alert_key` -> `INVALID_ARGUMENT`; unknown app ->
`NOT_FOUND`; `registry` unreachable with nothing cached, or Postgres down
-> `UNAVAILABLE`. A Redis failure is not an error: the answer comes from
Postgres instead.
"""

import logging

import grpc

from app.core.service import MemoryService
from proto_gen import memory_store_pb2, memory_store_pb2_grpc
from registry_client import AppNotFoundError, RegistryUnavailableError

logger = logging.getLogger(__name__)


class MemoryStoreServicer(memory_store_pb2_grpc.MemoryStoreServicer):
    def __init__(self, service: MemoryService):
        self._service = service

    async def GetContext(
        self, request: memory_store_pb2.GetContextRequest, context: grpc.aio.ServicerContext
    ) -> memory_store_pb2.GetContextResponse:
        if not request.app_id or not request.alert_key:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "app_id and alert_key are required.")
        try:
            return await self._service.get_context(request.app_id, request.alert_key)
        except AppNotFoundError as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except RegistryUnavailableError as e:
            await context.abort(grpc.StatusCode.UNAVAILABLE, f"registry unavailable ({e})")
        except Exception:
            logger.exception("GetContext failed for %s/%s", request.app_id, request.alert_key)
            await context.abort(grpc.StatusCode.UNAVAILABLE, "memory unavailable")


async def start_grpc_server(service: MemoryService, port: int) -> grpc.aio.Server:
    server = grpc.aio.server()
    memory_store_pb2_grpc.add_MemoryStoreServicer_to_server(MemoryStoreServicer(service), server)
    server.add_insecure_port(f"[::]:{port}")  # plaintext inside the platform network (§13 T12)
    await server.start()
    logger.info("gRPC MemoryStore service listening on :%d", port)
    return server
