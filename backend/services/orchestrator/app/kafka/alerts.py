"""`alert.received` -> agent run -> `alert.decided` (docs/ARCHITECTURE.md §4).

Both payloads are the `agent.proto` messages, protobuf-encoded (§5); both
are keyed `{app_id}:{alert_key}` (ADR-0002). One message at a time, and the
offset is committed only after `alert.decided` is published, so a crash
re-runs the alert rather than losing it (duplicate handling is Day 16).

Every alert that decodes gets exactly one `alert.decided`, even when the run
fails: a failed run publishes ESCALATE with a "not evaluated" reason so a
human still sees it (§10). An undecodable message has no `alert_id` to
answer for; it's logged and skipped (DLQ: Day 16).
"""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaConnectionError
from google.protobuf.message import DecodeError

from app.agent.loop import AgentRunner, not_evaluated_response
from app.agent.llm import LLMError
from app.core.manifest import ManifestUnavailableError, ResolutionError
from app.tools.gateway import GatewayUnavailableError
from proto_gen import agent_pb2

logger = logging.getLogger(__name__)

ALERT_RECEIVED = "alert.received"
ALERT_DECIDED = "alert.decided"
CONSUMER_GROUP = "orchestrator"
RECONNECT_DELAY_S = 2.0


def message_key(app_id: str, alert_key: str) -> bytes:
    return f"{app_id}:{alert_key}".encode()


async def handle_alert(raw: bytes, runner: AgentRunner) -> tuple[bytes, agent_pb2.RunAgentResponse] | None:
    """Decide one `alert.received` message: the `alert.decided` key and value
    to publish, or None when there's nothing to publish."""
    request = agent_pb2.RunAgentRequest()
    try:
        request.ParseFromString(raw)
    except DecodeError:
        logger.error("skipping undecodable alert.received message (%d bytes)", len(raw))
        return None
    if not request.app_id or not request.alert_id:
        logger.error("skipping alert.received message without app_id/alert_id")
        return None

    key = message_key(request.app_id, request.alert_key)
    try:
        return key, await runner.run(request)
    except ResolutionError as e:
        why = str(e)
    except LLMError as e:
        why = f"LLM unavailable ({e})"
    except (ManifestUnavailableError, GatewayUnavailableError) as e:
        why = str(e)
    except Exception:
        logger.exception("agent run failed for %s/%s", request.app_id, request.alert_id)
        why = "agent run failed"
    logger.warning("alert %s/%s not evaluated: %s", request.app_id, request.alert_id, why)
    return key, not_evaluated_response(request, why)


class AlertPipeline:
    """Consumes one message at a time (so one key at a time, in partition
    order) and commits only after its `alert.decided` is published. Topic and
    group names are parameters for the integration test only."""

    def __init__(
        self,
        runner: AgentRunner,
        bootstrap_servers: str,
        *,
        received_topic: str = ALERT_RECEIVED,
        decided_topic: str = ALERT_DECIDED,
        group_id: str = CONSUMER_GROUP,
    ):
        self._runner = runner
        self._bootstrap_servers = bootstrap_servers
        self._received_topic = received_topic
        self._decided_topic = decided_topic
        self._group_id = group_id
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever(), name="alert-pipeline")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        # Keep the service up (and /healthz green) while Kafka is unreachable;
        # reconnect until it's back.
        while True:
            try:
                await self._consume()
            except KafkaConnectionError as e:
                logger.warning("Kafka unavailable (%s); retrying in %.0fs", e, RECONNECT_DELAY_S)
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _consume(self) -> None:
        consumer = AIOKafkaConsumer(
            self._received_topic,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers, enable_idempotence=True, acks="all")
        await producer.start()
        try:
            await consumer.start()
            try:
                logger.info("consuming %s", self._received_topic)
                async for msg in consumer:
                    decided = await handle_alert(msg.value, self._runner)
                    if decided is not None:
                        key, response = decided
                        await producer.send_and_wait(self._decided_topic, value=response.SerializeToString(), key=key)
                    await consumer.commit()
            finally:
                await consumer.stop()
        finally:
            await producer.stop()
