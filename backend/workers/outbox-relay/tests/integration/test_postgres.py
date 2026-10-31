"""The relay's SQL against the Compose Postgres: the advisory lock, claims,
SKIP LOCKED, pruning. Runs in a throwaway schema (copies of the two tables)
with its own lock key, so the running `outbox-relay` never sees these rows.

`uv run pytest -m integration` while `backend/local` is up.
"""

import os
import uuid

import asyncpg
import pytest

from app.postgres import PostgresDatabase
from app.relay import OUTBOX, VERDICT_OUTBOX, Relay
from tests.conftest import FakeProducer

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

DSN = os.environ.get("POSTGRES_DSN", "postgresql://platform:platform@localhost:5432/platform")
LOCK_KEY = 990_001  # not the real relay's


@pytest.fixture
async def schema():
    name = f"relay_it_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(DSN)
    await conn.execute(f"CREATE SCHEMA {name}")
    for table in ("outbox", "verdict_outbox"):
        await conn.execute(f"CREATE TABLE {name}.{table} (LIKE public.{table} INCLUDING ALL)")
    try:
        yield name, conn
    finally:
        await conn.execute(f"DROP SCHEMA {name} CASCADE")
        await conn.close()


def db_for(schema_name: str) -> PostgresDatabase:
    # asyncpg passes unknown DSN parameters as server settings.
    return PostgresDatabase(f"{DSN}?search_path={schema_name}", LOCK_KEY)


async def insert(conn, schema_name: str, key: str, payload: bytes, table: str = "outbox", **cols) -> int:
    ref = ("alert_id", "a") if table == "outbox" else ("case_id", 1)
    extra = "".join(f", {c}" for c in cols)
    params = "".join(f", ${i + 7}" for i in range(len(cols)))
    return await conn.fetchval(
        f"INSERT INTO {schema_name}.{table} (app_id, {ref[0]}, topic, kafka_key, payload, headers{extra}) "
        f"VALUES ($1, $2, $3, $4, $5, $6{params}) RETURNING id",
        "app", ref[1], "alert.received" if table == "outbox" else "verdict.recorded", key, payload,
        '{"traceparent": "00-x-y-01"}', *cols.values(),
    )


async def test_only_one_session_holds_the_lock_and_closing_releases_it(schema):
    name, _ = schema
    db = db_for(name)
    a, b = await db.connect(), await db.connect()
    try:
        assert await a.try_lock() is True
        assert await b.try_lock() is False
        await a.close()
        assert await b.try_lock() is True
    finally:
        await b.close()


async def test_a_terminated_connection_loses_the_lock(schema):
    name, admin = schema
    db = db_for(name)
    a, b = await db.connect(), await db.connect()
    try:
        assert await a.try_lock()
        pid = a._conn.get_server_pid()
        await admin.execute("SELECT pg_terminate_backend($1)", pid)
        from app.relay import DatabaseUnavailable

        with pytest.raises(DatabaseUnavailable):
            await a.try_lock()
        assert await b.try_lock() is True
    finally:
        await a.close()
        await b.close()


async def test_drain_publishes_in_id_order_and_marks_sent(schema):
    name, conn = schema
    for i in range(3):
        await insert(conn, name, "app:k", f"m{i}".encode())
    await insert(conn, name, "app:k", b"v0", table="verdict_outbox")
    producer = FakeProducer()
    session = await db_for(name).connect()
    try:
        assert await session.try_lock()
        await Relay(db_for(name), producer).drain_once(session)
    finally:
        await session.close()

    assert [s.value for s in producer.sent] == [b"m0", b"m1", b"m2", b"v0"]
    assert producer.sent[0].headers == [("traceparent", b"00-x-y-01")]
    created = await conn.fetchval(f"SELECT created_at FROM {name}.verdict_outbox")
    assert producer.sent[3].timestamp_ms == int(created.timestamp() * 1000)
    rows = await conn.fetch(f"SELECT status, sent_at FROM {name}.outbox ORDER BY id")
    assert all(r["status"] == "SENT" and r["sent_at"] is not None for r in rows)


async def test_kafka_failure_commits_what_was_sent_and_leaves_the_rest(schema):
    name, conn = schema
    for i in range(3):
        await insert(conn, name, "app:k", f"m{i}".encode())
    producer = FakeProducer(fail_after=1)
    session = await db_for(name).connect()
    try:
        await session.try_lock()
        from app.relay import KafkaUnavailable

        with pytest.raises(KafkaUnavailable):
            await Relay(db_for(name), producer).drain_table(session, OUTBOX)
    finally:
        await session.close()
    rows = await conn.fetch(f"SELECT status, attempts FROM {name}.outbox ORDER BY id")
    assert [(r["status"], r["attempts"]) for r in rows] == [("SENT", 0), ("PENDING", 0), ("PENDING", 0)]


async def test_claim_skips_rows_another_transaction_holds(schema):
    name, conn = schema
    first = await insert(conn, name, "app:k", b"m0")
    await insert(conn, name, "app:k", b"m1")
    session = await db_for(name).connect()
    try:
        async with conn.transaction():
            await conn.execute(f"SELECT 1 FROM {name}.outbox WHERE id = $1 FOR UPDATE", first)
            async with session.claim(OUTBOX, 10) as claim:
                assert [r.payload for r in claim.rows] == [b"m1"]
    finally:
        await session.close()


async def test_prune_deletes_only_sent_rows_past_retention(schema):
    name, conn = schema
    old = "now() - interval '25 hours'"
    await insert(conn, name, "app:k", b"old-sent", status="SENT")
    await insert(conn, name, "app:k", b"recent-sent", status="SENT")
    await insert(conn, name, "app:k", b"pending")
    await insert(conn, name, "app:k", b"old-dead", status="DEAD")
    await conn.execute(f"UPDATE {name}.outbox SET sent_at = {old} WHERE payload = 'old-sent'")
    await conn.execute(f"UPDATE {name}.outbox SET sent_at = now() WHERE payload = 'recent-sent'")
    await conn.execute(f"UPDATE {name}.outbox SET created_at = {old} WHERE payload IN ('pending', 'old-dead')")
    await insert(conn, name, "app:k", b"old-verdict", table="verdict_outbox", status="SENT")
    await conn.execute(f"UPDATE {name}.verdict_outbox SET sent_at = {old}")

    session = await db_for(name).connect()
    try:
        assert await session.prune(OUTBOX, 24) == 1
        assert await session.prune(VERDICT_OUTBOX, 24) == 1
    finally:
        await session.close()
    left = await conn.fetch(f"SELECT payload FROM {name}.outbox ORDER BY id")
    assert [bytes(r["payload"]) for r in left] == [b"recent-sent", b"pending", b"old-dead"]
