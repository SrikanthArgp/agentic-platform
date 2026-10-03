"""The `Agent` gRPC service (`agent.proto` `RunAgent`), the synchronous twin
of the `alert.received` -> `alert.decided` Kafka path (docs/ARCHITECTURE.md §5).

An unknown app or agent is the caller's error (`NOT_FOUND`). A run that
fails for any other reason still answers, with ESCALATE and a "not
evaluated" reason, same as the Kafka path.
"""

import logging

import grpc

from app.agent.llm import LLMError
from app.agent.loop import AgentRunner, not_evaluated_response
from app.core.manifest import ResolutionError
from proto_gen import agent_pb2, agent_pb2_grpc

logger = logging.getLogger(__name__)


class AgentServicer(agent_pb2_grpc.AgentServicer):
    def __init__(self, runner: AgentRunner):
        self._runner = runner

    async def RunAgent(
        self, request: agent_pb2.RunAgentRequest, context: grpc.aio.ServicerContext
    ) -> agent_pb2.RunAgentResponse:
        try:
            return await self._runner.run(request)
        except ResolutionError as e:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(e))
        except LLMError as e:
            return not_evaluated_response(request, f"LLM unavailable ({e})")
        except Exception:
            logger.exception("RunAgent failed for %s/%s", request.app_id, request.alert_id)
            return not_evaluated_response(request, "agent run failed")


async def start_grpc_server(runner: AgentRunner, port: int) -> grpc.aio.Server:
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(AgentServicer(runner), server)
    server.add_insecure_port(f"[::]:{port}")  # plaintext inside the platform network (§13 T12)
    await server.start()
    logger.info("gRPC Agent service listening on :%d", port)
    return server
