"""`alert.received` -> agent run -> `alert.decided` (docs/ARCHITECTURE.md §4).

Both payloads are the `agent.proto` messages, protobuf-encoded (§5); both
are keyed `{app_id}:{alert_key}` (ADR-0002). One message at a time, and its
offset is committed only after its `alert.decided` is published, so a crash
re-runs the alert rather than losing it. Delivery is at-least-once, so an
alert whose decision was already published is skipped (`SeenAlerts`,
`seen:{app_id}:{alert_id}` in Redis, Day 16).

Pod swaps (Day 19, ADR-0033): partitions move between replicas when one
joins or leaves the group. aiokafka has no cooperative (incremental)
rebalancing, so every rebalance revokes every member's partitions; the
sticky assignor at least hands most of them back to the same members.
Before giving up its partitions, a member finishes and commits the
alert it's on (`on_partitions_revoked` waits for it) and takes no new
one until it has its new assignment, so the next owner never starts an
alert this pod is still running. A message fetched but not started is
never committed, and is redelivered to whoever owns its partition next.
On shutdown (`stop()`) the pipeline finishes the current alert, commits,
and leaves the group, so the others rebalance at once rather than after a
session timeout.

Every alert that decodes gets exactly one `alert.decided`, even when the run
fails: a failed run publishes ESCALATE with a "not evaluated" reason so a
human still sees it (§10). An undecodable message has no `alert_id` to
answer for; it's logged and skipped. (The `alert.received.dlq` topic is for
rows the `outbox-relay` couldn't publish, not for this consumer.)
"""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRebalanceListener, TopicPartition
from aiokafka.coordinator.assignors.sticky.sticky_assignor import StickyPartitionAssignor
from aiokafka.errors import KafkaConnectionError, KafkaError
from aiokafka.structs import OffsetAndMetadata
from google.protobuf.message import DecodeError

from app.agent.loop import AgentRunner, RunTimeoutError, not_evaluated_response
from app.agent.llm import LLMError
from app.core.manifest import ManifestUnavailableError, ResolutionError
from app.governance.budget import BudgetExceededError
from app.kafka.dedupe import SeenAlerts
from app.tools.gateway import GatewayUnavailableError
from proto_gen import agent_pb2

logger = logging.getLogger(__name__)

ALERT_RECEIVED = "alert.received"
ALERT_DECIDED = "alert.decided"
CONSUMER_GROUP = "orchestrator"
RECONNECT_DELAY_S = 2.0
# How long one poll waits for a message before checking for stop().
POLL_TIMEOUT_MS = 500


def message_key(app_id: str, alert_key: str) -> bytes:
    return f"{app_id}:{alert_key}".encode()


async def handle_alert(
    raw: bytes, runner: AgentRunner, seen: SeenAlerts | None = None
) -> tuple[bytes, agent_pb2.RunAgentResponse] | None:
    """Decide one `alert.received` message: the `alert.decided` key and value
    to publish, or None when there's nothing to publish (undecodable, or a
    duplicate of an alert already decided)."""
    request = agent_pb2.RunAgentRequest()
    try:
        request.ParseFromString(raw)
    except DecodeError:
        logger.error("skipping undecodable alert.received message (%d bytes)", len(raw))
        return None
    if not request.app_id or not request.alert_id:
        logger.error("skipping alert.received message without app_id/alert_id")
        return None
    if seen is not None and await seen.seen(request.app_id, request.alert_id):
        logger.info("skipping duplicate alert %s/%s: already decided", request.app_id, request.alert_id)
        return None

    key = message_key(request.app_id, request.alert_key)
    try:
        return key, await runner.run(request)
    except ResolutionError as e:
        why = str(e)
    except LLMError as e:
        why = f"LLM unavailable ({e})"
    except (ManifestUnavailableError, GatewayUnavailableError, BudgetExceededError, RunTimeoutError) as e:
        why = str(e)
    except Exception:
        logger.exception("agent run failed for %s/%s", request.app_id, request.alert_id)
        why = "agent run failed"
    logger.warning("alert %s/%s not evaluated: %s", request.app_id, request.alert_id, why)
    return key, not_evaluated_response(request, why)


class AlertPipeline:
    """Consumes one message at a time (so one key at a time, in partition
    order) and commits it only after its `alert.decided` is published.
    Topic and group names are parameters for the integration test only;
    `consumer_factory`/`producer_factory` are for unit tests."""

    def __init__(
        self,
        runner: AgentRunner,
        bootstrap_servers: str,
        *,
        seen: SeenAlerts | None = None,
        received_topic: str = ALERT_RECEIVED,
        decided_topic: str = ALERT_DECIDED,
        group_id: str = CONSUMER_GROUP,
        consumer_factory=None,
        producer_factory=None,
    ):
        self._runner = runner
        self._seen = seen
        self._bootstrap_servers = bootstrap_servers
        self._received_topic = received_topic
        self._decided_topic = decided_topic
        self._group_id = group_id
        self._consumer_factory = consumer_factory or self._kafka_consumer
        self._producer_factory = producer_factory or self._kafka_producer
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        # Set while no alert is being run; on_partitions_revoked waits on it.
        self._idle = asyncio.Event()
        self._idle.set()
        # Between "partitions revoked" and "partitions assigned": take nothing new.
        self._rebalancing = False
        self.joined = False

    @property
    def ready(self) -> bool:
        """The consumer has joined its group (has an assignment, maybe empty)."""
        return self.joined and not self._rebalancing

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever(), name="alert-pipeline")

    async def stop(self, timeout_s: float = 135.0) -> None:
        """Finish the current alert (publish, commit), leave the group, return.
        Cancels only if that takes longer than `timeout_s`; the alert is then
        uncommitted and runs again on another replica."""
        self._stopping.set()
        if not self._task:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout_s)
        except TimeoutError:
            logger.warning("alert pipeline didn't drain within %.0fs; cancelling", timeout_s)
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        # Keep the service up (and /healthz green) while Kafka is unreachable;
        # reconnect until it's back.
        while not self._stopping.is_set():
            try:
                await self._consume()
            except KafkaError as e:
                self.joined = False
                logger.warning("Kafka error (%s); reconnecting in %.0fs", e, RECONNECT_DELAY_S)
                try:
                    await asyncio.wait_for(self._stopping.wait(), RECONNECT_DELAY_S)
                except TimeoutError:
                    pass

    def _kafka_consumer(self, listener: ConsumerRebalanceListener):
        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            partition_assignment_strategy=(StickyPartitionAssignor,),
        )
        consumer.subscribe([self._received_topic], listener=listener)
        return consumer

    def _kafka_producer(self):
        return AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers, enable_idempotence=True, acks="all")

    async def _consume(self) -> None:
        consumer = self._consumer_factory(_Rebalance(self))
        producer = self._producer_factory()
        await producer.start()
        try:
            await consumer.start()
            try:
                logger.info("consuming %s", self._received_topic)
                while not self._stopping.is_set():
                    batch = await consumer.getmany(timeout_ms=POLL_TIMEOUT_MS, max_records=1)
                    for msg in (m for msgs in batch.values() for m in msgs):
                        # No await between this check and clearing _idle: a
                        # revoke either happened before (skip; redelivered)
                        # or will wait for this alert to be committed.
                        if self._rebalancing or self._stopping.is_set():
                            break
                        self._idle.clear()
                        try:
                            await self._process(consumer, producer, msg)
                        finally:
                            self._idle.set()
            finally:
                # Leaves the group (LeaveGroup), so the rest rebalance now.
                await consumer.stop()
                self.joined = False
        finally:
            await producer.stop()

    async def _process(self, consumer, producer, msg) -> None:
        decided = await handle_alert(msg.value, self._runner, self._seen)
        if decided is not None:
            key, response = decided
            # Raises on failure: then nothing is committed and the alert is redelivered.
            await producer.send_and_wait(self._decided_topic, value=response.SerializeToString(), key=key)
            if self._seen is not None:
                await self._seen.mark(response.app_id, response.alert_id)
        await consumer.commit({TopicPartition(msg.topic, msg.partition): OffsetAndMetadata(msg.offset + 1, "")})


class _Rebalance(ConsumerRebalanceListener):
    def __init__(self, pipeline: AlertPipeline):
        self._pipeline = pipeline

    async def on_partitions_revoked(self, revoked) -> None:
        # Awaited by aiokafka before it rejoins: the group waits for us.
        self._pipeline._rebalancing = True
        await self._pipeline._idle.wait()

    async def on_partitions_assigned(self, assigned) -> None:
        self._pipeline._rebalancing = False
        self._pipeline.joined = True
        logger.info("assigned %d partition(s): %s", len(assigned), sorted(tp.partition for tp in assigned))
