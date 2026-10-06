"""One Kafka consumer for both topics memory-store reads: `alert.decided`
(decisions, ADR-0016) and `verdict.recorded` (analyst verdicts, Day 10).

Own consumer group (`memory-store`), so it reads every message
independently of `review-console`. One message at a time, offset committed
only after the event is stored: per-key order (ADR-0002) keeps counts
correct across replicas, and a crash re-reads rather than loses.

Failures:
- an unusable message: the handler logs and skips it, and it's committed.
  Nothing can be recorded for it.
- anything else (`registry` or Postgres unreachable): retried with backoff,
  not committed, so no event is lost while a dependency is down.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaConnectionError

from app.core.service import MemoryService
from app.kafka.decisions import ALERT_DECIDED, handle_decided
from app.kafka.verdicts import VERDICT_RECORDED, handle_verdict

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "memory-store"
RECONNECT_DELAY_S = 2.0
RETRY_DELAYS_S = (0.5, 1, 2, 5, 10)

Handler = Callable[[bytes, int, MemoryService], Awaitable[bool]]
HANDLERS: Mapping[str, Handler] = {ALERT_DECIDED: handle_decided, VERDICT_RECORDED: handle_verdict}


class EventConsumer:
    """`handlers` (topic -> handler) and the group are parameters for tests only."""

    def __init__(
        self,
        service: MemoryService,
        bootstrap_servers: str,
        *,
        handlers: Mapping[str, Handler] = HANDLERS,
        group_id: str = CONSUMER_GROUP,
    ):
        self._service = service
        self._bootstrap_servers = bootstrap_servers
        self._handlers = handlers
        self._group_id = group_id
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever(), name="event-consumer")

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
            *self._handlers,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await consumer.start()
        try:
            logger.info("consuming %s as %s", ", ".join(self._handlers), self._group_id)
            async for msg in consumer:
                await self._handle_with_retry(self._handlers[msg.topic], msg.value, msg.timestamp)
                await consumer.commit()
        finally:
            await consumer.stop()

    async def _handle_with_retry(self, handler: Handler, raw: bytes, timestamp_ms: int) -> None:
        attempt = 0
        while True:
            try:
                await handler(raw, timestamp_ms, self._service)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
                logger.exception("recording an event failed; retrying in %.1fs", delay)
                attempt += 1
                await asyncio.sleep(delay)
