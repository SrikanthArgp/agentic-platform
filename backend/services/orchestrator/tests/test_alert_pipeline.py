"""`AlertPipeline` over a fake consumer and producer (docs/plan.md Day 19):
commit only after a successful publish, finish the current alert on stop,
and never give up a partition mid-alert."""

import asyncio
from types import SimpleNamespace

import pytest
from aiokafka import TopicPartition
from aiokafka.errors import KafkaTimeoutError

from app.kafka.alerts import ALERT_DECIDED, ALERT_RECEIVED, AlertPipeline
from proto_gen import agent_pb2

pytestmark = pytest.mark.anyio

TP = TopicPartition(ALERT_RECEIVED, 0)


def message(offset: int, alert_id: str) -> SimpleNamespace:
    request = agent_pb2.RunAgentRequest(app_id="test-app", alert_id=alert_id, alert_key="k")
    return SimpleNamespace(topic=ALERT_RECEIVED, partition=0, offset=offset, value=request.SerializeToString())


class GatedRunner:
    """Each run waits until `release()`; `started` is set once one is running."""

    def __init__(self, gated: bool = True):
        self.gate = asyncio.Event()
        if not gated:
            self.gate.set()
        self.started = asyncio.Event()
        self.runs: list[str] = []

    def release(self):
        self.gate.set()

    async def run(self, request):
        self.runs.append(request.alert_id)
        self.started.set()
        await self.gate.wait()
        return agent_pb2.RunAgentResponse(
            app_id=request.app_id, alert_id=request.alert_id, alert_key=request.alert_key, decision=agent_pb2.ESCALATE
        )


class FakeConsumer:
    def __init__(self, listener):
        self.listener = listener
        self.queue: asyncio.Queue = asyncio.Queue()
        self.commits: list[dict] = []
        self.stopped = False

    async def start(self):
        await self.listener.on_partitions_revoked(set())
        await self.listener.on_partitions_assigned({TP})

    async def getmany(self, timeout_ms, max_records):
        try:
            msg = await asyncio.wait_for(self.queue.get(), timeout_ms / 1000)
        except TimeoutError:
            return {}
        return {TP: [msg]}

    async def commit(self, offsets):
        self.commits.append({tp.partition: meta.offset for tp, meta in offsets.items()})

    async def stop(self):
        self.stopped = True


class FakeProducer:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_and_wait(self, topic, value, key):
        if self.fail:
            raise KafkaTimeoutError("broker unavailable")
        self.sent.append((topic, agent_pb2.RunAgentResponse.FromString(value).alert_id))


def make_pipeline(runner, producer=None):
    consumers: list[FakeConsumer] = []
    producer = producer or FakeProducer()

    def consumer_factory(listener):
        consumers.append(FakeConsumer(listener))
        return consumers[-1]

    pipeline = AlertPipeline(
        runner, "unused:9092", consumer_factory=consumer_factory, producer_factory=lambda: producer
    )
    return pipeline, consumers, producer


async def wait_for(condition, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def test_publishes_then_commits_the_next_offset():
    pipeline, consumers, producer = make_pipeline(GatedRunner(gated=False))
    pipeline.start()
    await wait_for(lambda: consumers and pipeline.ready)
    consumers[0].queue.put_nowait(message(7, "a-1"))

    await wait_for(lambda: consumers[0].commits)
    await pipeline.stop(timeout_s=2)

    assert producer.sent == [(ALERT_DECIDED, "a-1")]
    assert consumers[0].commits == [{0: 8}]


async def test_a_failed_publish_commits_nothing():
    runner = GatedRunner(gated=False)
    pipeline, consumers, producer = make_pipeline(runner, FakeProducer(fail=True))
    pipeline.start()
    await wait_for(lambda: consumers and pipeline.ready)
    consumers[0].queue.put_nowait(message(7, "a-1"))

    await wait_for(lambda: consumers[0].stopped)  # the Kafka error ends that consumer
    await pipeline.stop(timeout_s=2)

    assert runner.runs == ["a-1"]
    assert consumers[0].commits == []


async def test_stop_finishes_the_current_alert_then_leaves_the_group():
    runner = GatedRunner()
    pipeline, consumers, producer = make_pipeline(runner)
    pipeline.start()
    await wait_for(lambda: consumers and pipeline.ready)
    consumers[0].queue.put_nowait(message(3, "a-1"))
    await runner.started.wait()

    stopping = asyncio.create_task(pipeline.stop(timeout_s=5))
    await asyncio.sleep(0.2)
    assert not stopping.done()  # still running a-1
    assert consumers[0].commits == []

    runner.release()
    await stopping

    assert producer.sent == [(ALERT_DECIDED, "a-1")]
    assert consumers[0].commits == [{0: 4}]
    assert consumers[0].stopped  # consumer.stop() sends LeaveGroup
    assert pipeline.ready is False


async def test_stop_takes_no_new_alert():
    runner = GatedRunner()
    pipeline, consumers, producer = make_pipeline(runner)
    pipeline.start()
    await wait_for(lambda: consumers and pipeline.ready)
    consumers[0].queue.put_nowait(message(3, "a-1"))
    consumers[0].queue.put_nowait(message(4, "a-2"))
    await runner.started.wait()

    stopping = asyncio.create_task(pipeline.stop(timeout_s=5))
    runner.release()
    await stopping

    assert runner.runs == ["a-1"]  # a-2 is left uncommitted for the next owner
    assert consumers[0].commits == [{0: 4}]


async def test_revoke_waits_for_the_current_alert_and_takes_no_new_one():
    runner = GatedRunner()
    pipeline, consumers, producer = make_pipeline(runner)
    pipeline.start()
    await wait_for(lambda: consumers and pipeline.ready)
    consumer = consumers[0]
    consumer.queue.put_nowait(message(3, "a-1"))
    await runner.started.wait()

    revoking = asyncio.create_task(consumer.listener.on_partitions_revoked({TP}))
    consumer.queue.put_nowait(message(4, "a-2"))  # fetched during the rebalance
    await asyncio.sleep(0.2)
    assert not revoking.done()  # the group waits while a-1 runs
    assert pipeline.ready is False

    runner.release()
    await revoking
    assert consumer.commits == [{0: 4}]  # a-1 committed before the partition moved
    await asyncio.sleep(0.6)  # more than one poll
    assert runner.runs == ["a-1"]  # a-2 not started: its next owner runs it

    await pipeline.stop(timeout_s=2)


async def test_stop_gives_up_after_its_timeout():
    runner = GatedRunner()  # never released
    pipeline, consumers, _ = make_pipeline(runner)
    pipeline.start()
    await wait_for(lambda: consumers and pipeline.ready)
    consumers[0].queue.put_nowait(message(3, "a-1"))
    await runner.started.wait()

    await asyncio.wait_for(pipeline.stop(timeout_s=0.2), 2)

    assert consumers[0].commits == []  # uncommitted: another replica re-runs it
