"""Day 16: Kafka down vs. a rejected message; DEAD rows and the DLQ."""

import asyncio
import contextlib

import pytest

from app.relay import KafkaUnavailable, MessageRejected, Relay, Role
from tests.conftest import Sent, wait_for

pytestmark = pytest.mark.anyio

FAST = dict(poll_interval_s=0.001, lock_retry_s=0.001, reconnect_s=0.001, kafka_backoff_s=0.001)


async def locked(db):
    session = await db.connect()
    assert await session.try_lock()
    return session


async def test_kafka_down_keeps_rows_pending_without_counting_attempts(db, producer):
    db.add("outbox", "app:k", b"m0")
    producer.down = True
    session = await locked(db)
    r = Relay(db, producer, **FAST)
    for _ in range(10):
        with pytest.raises(KafkaUnavailable):
            await r.drain_once(session)
    [row] = db.tables["outbox"]
    assert (row.status, row.attempts, row.last_error) == ("PENDING", 0, None)


async def test_a_rejection_counts_an_attempt_and_holds_back_later_rows(db, producer):
    db.add("outbox", "app:k", b"bad")
    db.add("outbox", "app:k", b"next")
    producer.reject = {b"bad"}
    session = await locked(db)

    with pytest.raises(MessageRejected):
        await Relay(db, producer, **FAST).drain_once(session)

    bad, nxt = db.tables["outbox"]
    assert (bad.status, bad.attempts) == ("PENDING", 1)
    assert "MessageSizeTooLargeError" in bad.last_error
    assert nxt.status == "PENDING" and producer.sent == []  # didn't overtake it


async def test_five_rejections_make_the_row_dead_copy_it_to_the_dlq_and_move_on(db, producer):
    db.add("outbox", "it-ops-triage:disk_full:web-01", b"bad", headers={"traceparent": "00-a-b-01"})
    db.add("outbox", "it-ops-triage:disk_full:web-01", b"next")
    producer.reject = {b"bad"}
    session = await locked(db)
    r = Relay(db, producer, **FAST)

    for attempt in range(1, 5):
        with pytest.raises(MessageRejected):
            await r.drain_once(session)
        assert db.tables["outbox"][0].attempts == attempt
    # 5th attempt: alert.received rejects it again; the DLQ takes the copy.
    orig = producer.send

    async def send(topic, key, value, headers, ts):
        if topic == "alert.received.dlq":
            producer.sent.append(Sent(topic, key, value, headers, ts))
            return
        return await orig(topic, key, value, headers, ts)

    producer.send = send
    await r.drain_once(session)

    bad, nxt = db.tables["outbox"]
    assert (bad.status, bad.attempts) == ("DEAD", 5)
    assert nxt.status == "SENT"
    dlq, main = producer.sent
    assert (dlq.topic, dlq.key, dlq.value) == ("alert.received.dlq", b"it-ops-triage:disk_full:web-01", b"bad")
    headers = dict(dlq.headers)
    assert headers["outbox_id"] == str(bad.id).encode()
    assert headers["attempts"] == b"5"
    assert b"MessageSizeTooLargeError" in headers["last_error"]
    assert headers["traceparent"] == b"00-a-b-01"
    assert (main.topic, main.value) == ("alert.received", b"next")


async def test_payload_the_dlq_refuses_too_is_dead_lettered_without_it(db, producer):
    db.add("outbox", "app:k", b"huge", attempts=4)
    producer.reject = {b"huge"}
    producer.reject_payloads_on = {"alert.received.dlq"}
    session = await locked(db)

    await Relay(db, producer, **FAST).drain_once(session)

    assert db.tables["outbox"][0].status == "DEAD"
    [dlq] = producer.sent
    assert dlq.topic == "alert.received.dlq" and dlq.value == b""
    assert dict(dlq.headers)["payload_dropped"] == b"true"


async def test_a_dead_verdict_row_is_kept_with_no_dlq(db, producer, caplog):
    db.add("verdict_outbox", "app:k", b"bad", attempts=4)
    db.add("verdict_outbox", "app:k", b"next")
    producer.reject = {b"bad"}
    session = await locked(db)

    await Relay(db, producer, **FAST).drain_once(session)

    bad, nxt = db.tables["verdict_outbox"]
    assert (bad.status, bad.attempts) == ("DEAD", 5)
    assert nxt.status == "SENT"
    assert [s.topic for s in producer.sent] == ["verdict.recorded"]  # no DLQ topic
    assert any(r.levelname == "ERROR" and "no DLQ" in r.getMessage() for r in caplog.records)


async def test_kafka_dropping_during_the_dlq_copy_counts_no_attempt(db, producer):
    db.add("outbox", "app:k", b"bad", attempts=4)
    producer.reject = {b"bad"}
    session = await locked(db)
    orig = producer.send

    async def send(topic, key, value, headers, ts):
        if topic == "alert.received.dlq":
            raise KafkaUnavailable("broker gone")
        return await orig(topic, key, value, headers, ts)

    producer.send = send
    with pytest.raises(KafkaUnavailable):
        await Relay(db, producer, **FAST).drain_once(session)
    [row] = db.tables["outbox"]
    assert (row.status, row.attempts) == ("PENDING", 4)


async def test_run_loop_retries_a_rejected_row_until_dead_then_drains_the_rest(db, producer):
    db.add("outbox", "app:k", b"bad")
    db.add("outbox", "app:k", b"ok")
    producer.reject = {b"bad"}
    producer.reject_payloads_on = {"alert.received.dlq"}  # empty-body copy only
    r = Relay(db, producer, max_attempts=3, **FAST)
    task = asyncio.create_task(r.run())
    try:
        await wait_for(lambda: db.status("outbox") == ["DEAD", "SENT"])
        assert r.role is Role.ACTIVE
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert db.tables["outbox"][0].attempts == 3
    assert [(s.topic, s.value) for s in producer.sent] == [("alert.received.dlq", b""), ("alert.received", b"ok")]
