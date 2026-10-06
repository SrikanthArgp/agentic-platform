"""`alert.decided` -> a case for every `ESCALATE` decision (ARCHITECTURE §4 step 4).

Own consumer group (`review-console`), so it reads every decision
independently of `memory-store`. One message at a time, offset committed
only after the message is handled: a crash re-reads rather than loses, and
a redelivered decision finds its case already there (unique
`(app_id, alert_id)`).

Failures:
- undecodable, or missing `app_id`/`alert_id`/`alert_key`: logged, skipped,
  committed. No case can be built from it.
- anything else (Postgres unreachable): retried with backoff, not
  committed, so no escalation is lost while the database is down.
"""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaConnectionError
from google.protobuf.message import DecodeError

from app.core.service import CaseService
from proto_gen import agent_pb2

logger = logging.getLogger(__name__)

ALERT_DECIDED = "alert.decided"
CONSUMER_GROUP = "review-console"
RECONNECT_DELAY_S = 2.0
RETRY_DELAYS_S = (0.5, 1, 2, 5, 10)


async def handle_decided(raw: bytes, service: CaseService) -> bool:
    """Handle one `alert.decided` message. False if it was skipped as unusable."""
    response = agent_pb2.RunAgentResponse()
    try:
        response.ParseFromString(raw)
    except DecodeError:
        logger.error("skipping undecodable alert.decided message (%d bytes)", len(raw))
        return False
    if not (response.app_id and response.alert_id and response.alert_key):
        logger.error("skipping alert.decided without app_id/alert_id/alert_key (alert_id=%r)", response.alert_id)
        return False
    created = await service.record_decision(response)
    if created is not None:
        logger.info(
            "case for app_id=%s alert_key=%s alert_id=%s%s",
            response.app_id, response.alert_key, response.alert_id, "" if created else " (duplicate)",
        )
    return True


class DecisionConsumer:
    """Topic and group are parameters for the integration test only."""

    def __init__(
        self,
        service: CaseService,
        bootstrap_servers: str,
        *,
        topic: str = ALERT_DECIDED,
        group_id: str = CONSUMER_GROUP,
    ):
        self._service = service
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever(), name="decision-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while True:
            try:
                await self._consume()
            except KafkaConnectionError as e:
                logger.warning("Kafka unavailable (%s); retrying in %.0fs", e, RECONNECT_DELAY_S)
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def _consume(self) -> None:
        consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await consumer.start()
        try:
            logger.info("consuming %s as %s", self._topic, self._group_id)
            async for msg in consumer:
                await self._handle_with_retry(msg.value)
                await consumer.commit()
        finally:
            await consumer.stop()

    async def _handle_with_retry(self, raw: bytes) -> None:
        attempt = 0
        while True:
            try:
                await handle_decided(raw, self._service)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
                logger.exception("recording a case failed; retrying in %.1fs", delay)
                attempt += 1
                await asyncio.sleep(delay)
