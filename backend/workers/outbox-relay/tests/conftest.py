"""Fakes with the semantics the relay depends on: a session advisory lock
released when its connection closes or drops, claims that commit on a
clean exit and roll back otherwise, and a producer that can go down."""

import asyncio
import itertools
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.relay import DatabaseUnavailable, KafkaUnavailable, MessageRejected, OutboxRow, OutboxTable

T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class FakeRow:
    id: int
    topic: str
    kafka_key: str
    payload: bytes
    headers: dict[str, str]
    created_at: datetime
    status: str = "PENDING"
    attempts: int = 0
    sent_at: datetime | None = None
    last_error: str | None = None


class FakeDatabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[FakeRow]] = {"outbox": [], "verdict_outbox": []}
        self.lock_holder: "FakeSession | None" = None
        self.down = False
        self.now = T0 + timedelta(days=3)
        self._ids = itertools.count(1)
        self.sessions: list[FakeSession] = []

    def add(self, table: str, key: str, payload: bytes = b"x", *, topic: str | None = None,
            headers: dict[str, str] | None = None, created_at: datetime = T0, **extra) -> FakeRow:
        row = FakeRow(
            id=next(self._ids), topic=topic or ("alert.received" if table == "outbox" else "verdict.recorded"),
            kafka_key=key, payload=payload, headers=headers or {}, created_at=created_at, **extra,
        )
        self.tables[table].append(row)
        return row

    def status(self, table: str) -> list[str]:
        return [r.status for r in self.tables[table]]

    async def connect(self) -> "FakeSession":
        await asyncio.sleep(0)
        if self.down:
            raise DatabaseUnavailable("connection refused")
        session = FakeSession(self)
        self.sessions.append(session)
        return session


class _Claim:
    def __init__(self, rows: list[FakeRow]):
        self.rows = [
            OutboxRow(id=r.id, topic=r.topic, kafka_key=r.kafka_key, payload=r.payload,
                      headers=dict(r.headers), created_at=r.created_at, attempts=r.attempts)
            for r in rows
        ]
        self.marked: list[int] = []
        # row id -> (status, attempts, last_error), applied on commit
        self.updates: dict[int, tuple[str, int, str]] = {}

    async def mark_sent(self, ids: Sequence[int]) -> None:
        self.marked.extend(ids)

    async def mark_failed(self, row_id: int, error: str, attempts: int) -> None:
        self.updates[row_id] = ("PENDING", attempts, error)

    async def mark_dead(self, row_id: int, error: str, attempts: int) -> None:
        self.updates[row_id] = ("DEAD", attempts, error)


class FakeSession:
    def __init__(self, db: FakeDatabase):
        self._db = db
        self.closed = False

    def _check(self) -> None:
        if self.closed:
            raise DatabaseUnavailable("connection lost")

    def lose(self) -> None:
        """The connection drops: Postgres releases its session lock."""
        self.closed = True
        if self._db.lock_holder is self:
            self._db.lock_holder = None

    async def try_lock(self) -> bool:
        await asyncio.sleep(0)
        self._check()
        if self._db.lock_holder in (None, self):
            self._db.lock_holder = self
            return True
        return False

    @asynccontextmanager
    async def claim(self, table: OutboxTable, limit: int) -> AsyncIterator[_Claim]:
        await asyncio.sleep(0)
        self._check()
        rows = sorted((r for r in self._db.tables[table.name] if r.status == "PENDING"), key=lambda r: r.id)
        claim = _Claim(rows[:limit])
        yield claim
        self._check()  # the commit needs the connection too
        for row in self._db.tables[table.name]:
            if row.id in claim.marked:
                row.status, row.sent_at = "SENT", self._db.now
            if row.id in claim.updates:
                row.status, row.attempts, row.last_error = claim.updates[row.id]

    async def prune(self, table: OutboxTable, retention_h: float) -> int:
        self._check()
        cutoff = self._db.now - timedelta(hours=retention_h)
        rows = self._db.tables[table.name]
        keep = [r for r in rows if not (r.status == "SENT" and r.sent_at < cutoff)]
        self._db.tables[table.name] = keep
        return len(rows) - len(keep)

    async def close(self) -> None:
        self.lose()


@dataclass
class Sent:
    topic: str
    key: bytes
    value: bytes
    headers: list[tuple[str, bytes]]
    timestamp_ms: int | None


@dataclass
class FakeProducer:
    sent: list[Sent] = field(default_factory=list)
    down: bool = False
    # Accept this many more sends, then go down.
    fail_after: int | None = None
    # Payloads Kafka refuses (MessageRejected), on any topic.
    reject: set[bytes] = field(default_factory=set)
    # Topics that refuse every non-empty payload (a DLQ that's too small too).
    reject_payloads_on: set[str] = field(default_factory=set)
    rejected: list[Sent] = field(default_factory=list)

    async def send(self, topic, key, value, headers, timestamp_ms) -> None:
        await asyncio.sleep(0)
        if not self.down and (value in self.reject or (value and topic in self.reject_payloads_on)):
            self.rejected.append(Sent(topic, key, value, headers, timestamp_ms))
            raise MessageRejected("MessageSizeTooLargeError: too large")
        if self.fail_after is not None:
            if self.fail_after == 0:
                self.down = True
            self.fail_after -= 1
        if self.down:
            raise KafkaUnavailable("broker unreachable")
        self.sent.append(Sent(topic, key, value, headers, timestamp_ms))


@pytest.fixture
def db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def producer() -> FakeProducer:
    return FakeProducer()


async def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)
