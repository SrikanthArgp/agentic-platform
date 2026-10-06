"""Publishes `verdict.recorded` (idempotent producer, acks=all; ADR-0002).

Day 9 publishes inline, inside the verdict's transaction: the verdict
commits only once Kafka has the event, and `POST /cases/{id}/verdict`
returns 503 if Kafka is unreachable, so a verdict is never recorded and
then lost to `memory-store`. Day 15's outbox (docs/ARCHITECTURE.md §10)
replaces this. Same producer as `ingestion`'s.
"""

import asyncio
import logging
from typing import Protocol

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

logger = logging.getLogger(__name__)

VERDICT_RECORDED = "verdict.recorded"
RECONNECT_DELAY_S = 2.0
SEND_TIMEOUT_S = 10.0


class PublishError(RuntimeError):
    """Kafka didn't take the message; the caller should retry later."""


class Publisher(Protocol):
    async def publish(self, topic: str, key: bytes, value: bytes) -> None: ...


class KafkaPublisher:
    def __init__(self, bootstrap_servers: str):
        self._bootstrap_servers = bootstrap_servers
        self._producer: AIOKafkaProducer | None = None
        self._connect_task: asyncio.Task | None = None

    def start(self) -> None:
        """Connect in the background, retrying, so the service starts even if Kafka is down."""
        self._connect_task = asyncio.create_task(self._connect(), name="kafka-publisher-connect")

    async def _connect(self) -> None:
        while True:
            producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers, enable_idempotence=True, acks="all"
            )
            try:
                await producer.start()
            except KafkaError as e:
                await producer.stop()
                logger.warning("Kafka unavailable (%s); retrying in %.0fs", e, RECONNECT_DELAY_S)
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue
            self._producer = producer
            logger.info("Kafka producer connected")
            return

    async def stop(self) -> None:
        if self._connect_task:
            self._connect_task.cancel()
        if self._producer:
            await self._producer.stop()

    async def publish(self, topic: str, key: bytes, value: bytes) -> None:
        if self._producer is None:
            raise PublishError("Kafka producer not connected yet.")
        try:
            await asyncio.wait_for(self._producer.send_and_wait(topic, value=value, key=key), SEND_TIMEOUT_S)
        except (KafkaError, TimeoutError) as e:
            raise PublishError(f"Kafka publish failed: {type(e).__name__}") from e
