"""The relay loop against fakes: order, keys, the lock, Kafka outages, pruning."""

import asyncio
import contextlib
from datetime import timedelta

import pytest

from app.relay import OUTBOX, VERDICT_OUTBOX, Relay, Role
from tests.conftest import T0, wait_for

pytestmark = pytest.mark.anyio

FAST = dict(poll_interval_s=0.001, lock_retry_s=0.001, reconnect_s=0.001, kafka_backoff_s=0.001)


def relay(db, producer, **kw) -> Relay:
    return Relay(db, producer, **(FAST | kw))


@contextlib.asynccontextmanager
async def running(r: Relay):
    task = asyncio.create_task(r.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_write_then_relay_round_trip_for_both_tables(db, producer):
    db.add("outbox", "it-ops-triage:disk_full:web-01", b"alert", headers={"traceparent": "00-ab-cd-01"})
    db.add("verdict_outbox", "it-ops-triage:disk_full:web-01", b'{"case_id":1}')

    session = await db.connect()
    assert await session.try_lock()
    assert await relay(db, producer).drain_once(session) is False

    assert [(s.topic, s.key, s.value) for s in producer.sent] == [
        ("alert.received", b"it-ops-triage:disk_full:web-01", b"alert"),
        ("verdict.recorded", b"it-ops-triage:disk_full:web-01", b'{"case_id":1}'),
    ]
    assert producer.sent[0].headers == [("traceparent", b"00-ab-cd-01")]
    assert db.status("outbox") == db.status("verdict_outbox") == ["SENT"]


async def test_a_keys_rows_are_published_in_id_order_with_the_stored_key(db, producer):
    for i in range(5):
        db.add("outbox", "app:key-a" if i % 2 == 0 else "app:key-b", f"m{i}".encode())

    session = await db.connect()
    await session.try_lock()
    await relay(db, producer, batch_size=2).drain_once(session)  # m0, m1
    await relay(db, producer, batch_size=2).drain_once(session)  # m2, m3
    await relay(db, producer, batch_size=2).drain_once(session)  # m4

    assert [(s.key, s.value) for s in producer.sent] == [
        (b"app:key-a", b"m0"), (b"app:key-b", b"m1"), (b"app:key-a", b"m2"),
        (b"app:key-b", b"m3"), (b"app:key-a", b"m4"),
    ]


async def test_verdicts_carry_created_at_as_the_kafka_timestamp_alerts_dont(db, producer):
    recorded = T0 - timedelta(minutes=42)
    db.add("outbox", "app:k", created_at=recorded)
    db.add("verdict_outbox", "app:k", created_at=recorded)

    session = await db.connect()
    await session.try_lock()
    await relay(db, producer).drain_once(session)

    alert, verdict = producer.sent
    assert alert.timestamp_ms is None
    assert verdict.timestamp_ms == int(recorded.timestamp() * 1000)


async def test_kafka_down_mid_batch_keeps_the_rest_pending_without_attempts(db, producer):
    for i in range(4):
        db.add("outbox", "app:k", f"m{i}".encode())
    producer.fail_after = 2

    session = await db.connect()
    await session.try_lock()
    r = relay(db, producer)
    from app.relay import KafkaUnavailable

    with pytest.raises(KafkaUnavailable):
        await r.drain_once(session)
    assert db.status("outbox") == ["SENT", "SENT", "PENDING", "PENDING"]
    assert [row.attempts for row in db.tables["outbox"]] == [0, 0, 0, 0]

    producer.down, producer.fail_after = False, None
    await r.drain_once(session)
    assert [s.value for s in producer.sent] == [b"m0", b"m1", b"m2", b"m3"]
    assert db.status("outbox") == ["SENT"] * 4


async def test_run_loop_backs_off_while_kafka_is_down_then_drains(db, producer):
    db.add("outbox", "app:k", b"m0")
    producer.down = True
    r = relay(db, producer)
    async with running(r):
        await wait_for(lambda: r.role is Role.ACTIVE)
        await asyncio.sleep(0.02)
        assert db.status("outbox") == ["PENDING"]
        producer.down = False
        await wait_for(lambda: db.status("outbox") == ["SENT"])


async def test_a_full_batch_loops_straight_away_an_empty_one_sleeps(db, producer):
    for i in range(5):
        db.add("outbox", "app:k", f"m{i}".encode())
    sleeps: list[float] = []
    session = await db.connect()

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(db.status("outbox")) == db.status("outbox").count("SENT"):
            raise asyncio.CancelledError  # stop once drained

    r = Relay(db, producer, batch_size=2, poll_interval_s=0.5, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await r.run_session(session)
    # Three batches (2, 2, 1) with no sleep in between; the first sleep is
    # the idle poll after the partial batch.
    assert len(producer.sent) == 5
    assert sleeps == [0.5]


async def test_second_relay_drains_nothing_while_the_first_holds_the_lock(db, producer):
    first, second = relay(db, producer), relay(db, producer)
    async with running(first) as first_task:
        await wait_for(lambda: first.role is Role.ACTIVE)
        async with running(second):
            await wait_for(lambda: second.role is Role.STANDBY)
            first_session = db.lock_holder
            db.add("outbox", "app:k", b"m0")
            await wait_for(lambda: db.status("outbox") == ["SENT"])
            assert db.lock_holder is first_session  # the first relayed it

            # The first stops (its connection closes): the second takes over.
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first_task
            await wait_for(lambda: second.role is Role.ACTIVE)
            db.add("outbox", "app:k", b"m1")
            await wait_for(lambda: db.status("outbox") == ["SENT", "SENT"])
            assert db.lock_holder is not first_session
    assert [s.value for s in producer.sent] == [b"m0", b"m1"]


async def test_a_standby_relay_never_claims(db, producer):
    holder = await db.connect()
    assert await holder.try_lock()
    db.add("outbox", "app:k")
    r = relay(db, producer)
    async with running(r):
        await wait_for(lambda: r.role is Role.STANDBY)
        await asyncio.sleep(0.02)
    assert producer.sent == [] and db.status("outbox") == ["PENDING"]


async def test_losing_the_locks_connection_stops_draining(db, producer):
    r = relay(db, producer)
    async with running(r):
        await wait_for(lambda: r.role is Role.ACTIVE)
        db.down = True  # it can't reconnect either
        db.lock_holder.lose()
        await wait_for(lambda: r.role is Role.DISCONNECTED)
        db.add("outbox", "app:k")
        await asyncio.sleep(0.02)
        assert producer.sent == [] and db.status("outbox") == ["PENDING"]

        db.down = False  # back: it takes the lock again and drains
        await wait_for(lambda: db.status("outbox") == ["SENT"])


async def test_connection_lost_before_commit_republishes_at_least_once(db, producer):
    db.add("outbox", "app:k", b"m0")
    session = await db.connect()
    await session.try_lock()

    real_send = producer.send

    async def send_then_drop(*args):
        await real_send(*args)
        session.lose()

    producer.send = send_then_drop
    from app.relay import DatabaseUnavailable

    with pytest.raises(DatabaseUnavailable):
        await relay(db, producer).drain_once(session)
    assert db.status("outbox") == ["PENDING"]  # rolled back: it will be sent again

    producer.send = real_send
    again = await db.connect()
    await again.try_lock()
    await relay(db, producer).drain_once(again)
    assert [s.value for s in producer.sent] == [b"m0", b"m0"]


async def test_pruning_deletes_only_sent_rows_past_retention(db, producer):
    old, recent = db.now - timedelta(hours=25), db.now - timedelta(hours=23)
    for table in ("outbox", "verdict_outbox"):
        db.add(table, "app:k", b"old-sent", status="SENT", sent_at=old)
        db.add(table, "app:k", b"recent-sent", status="SENT", sent_at=recent)
        db.add(table, "app:k", b"old-pending", created_at=old)
        db.add(table, "app:k", b"old-dead", status="DEAD", created_at=old)

    session = await db.connect()
    await relay(db, producer, retention_h=24).prune_if_due(session)

    for table in ("outbox", "verdict_outbox"):
        assert [r.payload for r in db.tables[table]] == [b"recent-sent", b"old-pending", b"old-dead"]


async def test_pruning_runs_once_per_interval(db, producer):
    now = [0.0]
    r = relay(db, producer, retention_h=24, prune_interval_s=300, clock=lambda: now[0])
    session = await db.connect()
    await r.prune_if_due(session)

    db.add("outbox", "app:k", status="SENT", sent_at=db.now - timedelta(hours=30))
    now[0] = 299
    await r.prune_if_due(session)
    assert len(db.tables["outbox"]) == 1
    now[0] = 300
    await r.prune_if_due(session)
    assert db.tables["outbox"] == []


def test_tables_and_their_timestamps():
    assert (OUTBOX.name, OUTBOX.timestamp_from_created_at) == ("outbox", False)
    assert (VERDICT_OUTBOX.name, VERDICT_OUTBOX.timestamp_from_created_at) == ("verdict_outbox", True)


async def test_an_unexpected_error_ends_the_session_not_the_worker(db, producer):
    db.add("outbox", "app:k", b"m0")
    r = relay(db, producer)
    calls = {"n": 0}
    orig = db.connect

    async def connect():
        session = await orig()
        calls["n"] += 1
        if calls["n"] == 1:
            async def boom():
                raise RuntimeError("driver bug")
            session.try_lock = boom
        return session

    db.connect = connect
    async with running(r):
        await wait_for(lambda: db.status("outbox") == ["SENT"])
    assert calls["n"] >= 2
