"""Which send errors count against a row (Day 16)."""

import pytest
from aiokafka.errors import (
    InvalidTopicError,
    KafkaConnectionError,
    KafkaTimeoutError,
    MessageSizeTooLargeError,
    NotLeaderForPartitionError,
    RecordListTooLargeError,
    TopicAuthorizationFailedError,
)

from app.kafka import KafkaProducer
from app.relay import KafkaUnavailable, MessageRejected

pytestmark = pytest.mark.anyio


class Raises:
    def __init__(self, error: Exception):
        self.error = error

    async def send_and_wait(self, *args, **kwargs):
        raise self.error


def producer(error: Exception) -> KafkaProducer:
    p = KafkaProducer("unused:9092")
    p._producer = Raises(error)
    return p


@pytest.mark.parametrize("error", [MessageSizeTooLargeError(), RecordListTooLargeError(), InvalidTopicError()])
async def test_errors_about_the_message_are_rejections(error):
    with pytest.raises(MessageRejected):
        await producer(error).send("t", b"k", b"v", [], None)


@pytest.mark.parametrize(
    "error",
    [KafkaConnectionError(), KafkaTimeoutError(), NotLeaderForPartitionError(), TopicAuthorizationFailedError(),
     TimeoutError()],
    ids=["connection", "timeout", "not-leader", "authorization", "deadline"],
)
async def test_everything_else_is_kafka_unavailable(error):
    """Authorization is non-retriable in aiokafka but would fail every row:
    rows wait instead of all going DEAD."""
    with pytest.raises(KafkaUnavailable):
        await producer(error).send("t", b"k", b"v", [], None)


async def test_not_connected_yet_is_unavailable():
    with pytest.raises(KafkaUnavailable):
        await KafkaProducer("unused:9092").send("t", b"k", b"v", [], None)
