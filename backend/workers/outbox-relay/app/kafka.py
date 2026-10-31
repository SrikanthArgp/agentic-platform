"""The relay's Kafka producer: idempotent, acks=all, as every service's (ADR-0002).

Connects in the background, retrying, so the worker starts while Kafka is
down; until then every send is `KafkaUnavailable` and rows stay `PENDING`.

A send error is `MessageRejected` only when it's about this one message
(Day 16): too large (aiokafka checks `max_request_size` before sending,
the broker checks `message.max.bytes`), corrupt, or an invalid topic name.
Everything else, including errors aiokafka calls non-retriable but that
would hit every message (e.g. authorization), is `KafkaUnavailable`: the
rows wait rather than burn attempts and go `DEAD` en masse.
"""

import asyncio
import logging

from aiokafka import AIOKafkaProducer
from aiokafka.errors import (
    CorruptRecordException,
    InvalidTopicError,
    KafkaError,
    MessageSizeTooLargeError,
    RecordListTooLargeError,
)

from app.relay import KafkaUnavailable, MessageRejected

logger = logging.getLogger(__name__)

MESSAGE_ERRORS = (MessageSizeTooLargeError, RecordListTooLargeError, CorruptRecordException, InvalidTopicError)

RECONNECT_DELAY_S = 2.0
SEND_TIMEOUT_S = 10.0


class KafkaProducer:
    def __init__(self, bootstrap_servers: str):
        self._bootstrap_servers = bootstrap_servers
        self._producer: AIOKafkaProducer | None = None
        self._connect_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._producer is not None

    def start(self) -> None:
        self._connect_task = asyncio.create_task(self._connect(), name="kafka-producer-connect")

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

    async def send(
        self, topic: str, key: bytes, value: bytes, headers: list[tuple[str, bytes]], timestamp_ms: int | None
    ) -> None:
        if self._producer is None:
            raise KafkaUnavailable("producer not connected yet")
        try:
            await asyncio.wait_for(
                self._producer.send_and_wait(topic, value=value, key=key, headers=headers, timestamp_ms=timestamp_ms),
                SEND_TIMEOUT_S,
            )
        except MESSAGE_ERRORS as e:
            raise MessageRejected(f"{type(e).__name__}: {e}") from e
        except (KafkaError, TimeoutError) as e:
            raise KafkaUnavailable(f"{type(e).__name__}") from e
