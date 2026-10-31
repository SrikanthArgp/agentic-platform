"""The relay loop: outbox tables -> Kafka, one active relay, in `id` order (ADR-0030).

Written once over a table and its rows' topics, and run over both
`outbox` (`ingestion`'s alerts -> `alert.received`) and `verdict_outbox`
(`review-console`'s verdicts -> `verdict.recorded`, ADR-0024).

- **One active relay.** A session holds a Postgres advisory lock and
  drains only while it does. Another instance waits for the lock instead
  of interleaving, so one key's messages keep their `id` order. If the
  session's connection drops, the lock goes with it and draining stops;
  the relay reconnects and competes for the lock again.
- **The loop.** Per table: claim up to `batch_size` `PENDING` rows in `id`
  order (`FOR UPDATE SKIP LOCKED`), publish each in order with its stored
  `kafka_key` and headers, and mark the published ones `SENT` in the same
  transaction. A full batch on either table loops straight away; otherwise
  sleep `poll_interval_s`.
- **Kafka unreachable.** Publishing stops at the first failure. Rows
  already published in that batch are marked `SENT`, the rest stay
  `PENDING` with no attempt counted, and the loop backs off. Day 16 tells
  a message-specific failure apart (attempts, `DEAD`, the DLQ); until
  then every send failure is treated as Kafka being unreachable.
- **Timestamps.** A table with `timestamp_from_created_at` (verdicts) is
  published with the row's `created_at` as the Kafka timestamp, because
  `memory-store` times a verdict by it; alerts keep the producer's time.
- **Pruning.** Every `prune_interval_s`, the active relay deletes `SENT`
  rows older than `retention_h` from each table. `DEAD` rows are kept.

Delivery is at-least-once: a crash after a publish and before the commit
re-publishes those rows. Consumers dedupe (Day 16).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxTable:
    name: str
    timestamp_from_created_at: bool = False


OUTBOX = OutboxTable("outbox")
VERDICT_OUTBOX = OutboxTable("verdict_outbox", timestamp_from_created_at=True)
TABLES = (OUTBOX, VERDICT_OUTBOX)


@dataclass(frozen=True)
class OutboxRow:
    id: int
    topic: str
    kafka_key: str
    payload: bytes
    headers: dict[str, str]
    created_at: datetime


class DatabaseUnavailable(RuntimeError):
    """Postgres can't be reached, or the session's connection was lost."""


class KafkaUnavailable(RuntimeError):
    """Kafka didn't take a message; nothing after it is published."""


class Claim(Protocol):
    rows: Sequence[OutboxRow]

    async def mark_sent(self, ids: Sequence[int]) -> None: ...


class Session(Protocol):
    """One Postgres connection: the advisory lock lives and dies with it."""

    async def try_lock(self) -> bool: ...

    def claim(self, table: OutboxTable, limit: int) -> AbstractAsyncContextManager[Claim]:
        """`PENDING` rows in `id` order, row-locked; commits on a clean exit."""
        ...

    async def prune(self, table: OutboxTable, retention_h: float) -> int:
        """Delete `SENT` rows older than `retention_h`; returns how many."""
        ...

    async def close(self) -> None: ...


class Database(Protocol):
    async def connect(self) -> Session: ...


class Producer(Protocol):
    async def send(
        self, topic: str, key: bytes, value: bytes, headers: list[tuple[str, bytes]], timestamp_ms: int | None
    ) -> None:
        """Raises `KafkaUnavailable` if the message wasn't acknowledged."""
        ...


class Role(StrEnum):
    STARTING = "starting"
    DISCONNECTED = "disconnected"  # no Postgres session
    STANDBY = "standby"  # connected, another relay holds the lock
    ACTIVE = "active"  # holds the lock, draining


class Relay:
    def __init__(
        self,
        db: Database,
        producer: Producer,
        *,
        tables: Sequence[OutboxTable] = TABLES,
        batch_size: int = 100,
        poll_interval_s: float = 0.5,
        retention_h: float = 24.0,
        prune_interval_s: float = 300.0,
        lock_retry_s: float = 2.0,
        reconnect_s: float = 2.0,
        kafka_backoff_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._db = db
        self._producer = producer
        self._tables = tables
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._retention_h = retention_h
        self._prune_interval_s = prune_interval_s
        self._lock_retry_s = lock_retry_s
        self._reconnect_s = reconnect_s
        self._kafka_backoff_s = kafka_backoff_s
        self._clock = clock
        self._sleep = sleep
        self._next_prune: float | None = None
        self.role = Role.STARTING

    async def run(self) -> None:
        """Forever: connect, wait for the lock, drain until the session is lost."""
        while True:
            try:
                session = await self._db.connect()
            except DatabaseUnavailable as e:
                self._set_role(Role.DISCONNECTED, f"Postgres unavailable ({e})")
                await self._sleep(self._reconnect_s)
                continue
            try:
                await self.run_session(session)
            except DatabaseUnavailable as e:
                self._set_role(Role.DISCONNECTED, f"session lost, draining stopped ({e})")
            finally:
                await session.close()
            await self._sleep(self._reconnect_s)

    async def run_session(self, session: Session) -> None:
        """Returns only by raising `DatabaseUnavailable` (or cancellation)."""
        while not await session.try_lock():
            self._set_role(Role.STANDBY, "another relay holds the lock; waiting")
            await self._sleep(self._lock_retry_s)
        self._set_role(Role.ACTIVE, "holding the relay lock; draining")
        self._next_prune = self._clock()
        while True:
            await self.prune_if_due(session)
            try:
                full = await self.drain_once(session)
            except KafkaUnavailable as e:
                logger.warning("Kafka unavailable (%s); rows stay PENDING, retrying in %.0fs", e, self._kafka_backoff_s)
                await self._sleep(self._kafka_backoff_s)
                continue
            if not full:
                await self._sleep(self._poll_interval_s)

    async def drain_once(self, session: Session) -> bool:
        """One batch per table. True if any batch was full (more is waiting)."""
        full = False
        for table in self._tables:
            full |= await self.drain_table(session, table) == self._batch_size
        return full

    async def drain_table(self, session: Session, table: OutboxTable) -> int:
        """Publish one batch in order; returns how many rows were claimed."""
        failure: KafkaUnavailable | None = None
        async with session.claim(table, self._batch_size) as claim:
            sent: list[int] = []
            for row in claim.rows:
                try:
                    await self._producer.send(
                        row.topic,
                        row.kafka_key.encode(),
                        row.payload,
                        [(k, v.encode()) for k, v in row.headers.items()],
                        int(row.created_at.timestamp() * 1000) if table.timestamp_from_created_at else None,
                    )
                except KafkaUnavailable as e:
                    # Stop here: publishing a later row would reorder its key.
                    failure = e
                    break
                sent.append(row.id)
            if sent:
                await claim.mark_sent(sent)
        if sent:
            logger.info("relayed %d %s row(s), ids %d..%d", len(sent), table.name, sent[0], sent[-1])
        if failure is not None:
            raise failure
        return len(claim.rows)

    async def prune_if_due(self, session: Session) -> None:
        if self._next_prune is not None and self._clock() < self._next_prune:
            return
        for table in self._tables:
            deleted = await session.prune(table, self._retention_h)
            if deleted:
                logger.info("pruned %d SENT %s row(s) older than %gh", deleted, table.name, self._retention_h)
        self._next_prune = self._clock() + self._prune_interval_s

    def _set_role(self, role: Role, why: str) -> None:
        if role is not self.role:
            log = logger.warning if role is Role.DISCONNECTED else logger.info
            log("relay %s -> %s: %s", self.role, role, why)
        self.role = role
