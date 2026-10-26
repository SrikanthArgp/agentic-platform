"""`ToolRunner`: deadline, retry/backoff, circuit breaker (Day 12)."""

import asyncio
import time

import pytest

from app.core.resilience import CircuitOpenError, CircuitState, ResiliencePolicy, ToolRunner, ToolTimeoutError

pytestmark = pytest.mark.anyio


class FakeTime:
    """`clock` + `sleep` for the runner: sleeping advances the clock instantly."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Flaky:
    """Fails the first `failures` attempts, then answers "ok"."""

    def __init__(self, failures: int):
        self.failures = failures
        self.attempts = 0

    async def __call__(self) -> str:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ConnectionError(f"attempt {self.attempts} failed")
        return "ok"


def runner(t: FakeTime, **policy) -> ToolRunner:
    defaults = dict(timeout_s=60, max_attempts=3, base_delay_s=0.1, max_delay_s=1.0, failure_threshold=3, cooldown_s=30)
    return ToolRunner(ResiliencePolicy(**defaults | policy), clock=t.clock, sleep=t.sleep, jitter=lambda: 1.0)


# --- retry / backoff ---


async def test_transient_failure_is_retried_with_exponential_backoff():
    t = FakeTime()
    tool = Flaky(failures=2)

    assert await runner(t).run("t", tool) == "ok"
    assert tool.attempts == 3
    assert t.sleeps == [pytest.approx(0.1), pytest.approx(0.2)]


async def test_gives_up_after_max_attempts_with_the_last_error():
    t = FakeTime()
    tool = Flaky(failures=10)

    with pytest.raises(ConnectionError, match="attempt 3 failed"):
        await runner(t).run("t", tool)
    assert tool.attempts == 3


def test_backoff_is_capped_and_jittered():
    t = FakeTime()
    r = runner(t, base_delay_s=0.1, max_delay_s=0.5)
    assert [r.delay_s(n) for n in range(1, 6)] == pytest.approx([0.1, 0.2, 0.4, 0.5, 0.5])

    jittered = ToolRunner(ResiliencePolicy(base_delay_s=0.1), jitter=lambda: 1.5)
    assert jittered.delay_s(1) == pytest.approx(0.15)


async def test_no_retry_that_would_end_past_the_deadline():
    t = FakeTime()
    tool = Flaky(failures=10)

    with pytest.raises(ConnectionError, match="attempt 1 failed"):
        await runner(t, timeout_s=0.05).run("t", tool)
    assert tool.attempts == 1
    assert t.sleeps == []


# --- timeout ---


async def test_slow_async_tool_times_out_at_the_deadline():
    r = ToolRunner(ResiliencePolicy(timeout_s=0.05))
    started = time.monotonic()

    with pytest.raises(ToolTimeoutError):
        await r.run("t", lambda: asyncio.sleep(10))
    assert time.monotonic() - started < 1


async def test_slow_sync_tool_in_a_thread_times_out_at_the_deadline():
    r = ToolRunner(ResiliencePolicy(timeout_s=0.05))
    started = time.monotonic()

    with pytest.raises(ToolTimeoutError):
        await r.run("t", lambda: asyncio.to_thread(time.sleep, 0.5))
    assert time.monotonic() - started < 0.4


async def test_timed_out_attempt_is_not_retried():
    attempts = 0

    async def slow():
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(10)

    with pytest.raises(ToolTimeoutError):
        await ToolRunner(ResiliencePolicy(timeout_s=0.05, max_attempts=3)).run("t", slow)
    assert attempts == 1


async def test_a_handlers_own_timeout_error_is_an_ordinary_failure():
    async def raises_timeout():
        raise TimeoutError("upstream socket timeout")

    t = FakeTime()
    with pytest.raises(TimeoutError, match="upstream socket timeout") as e:
        await runner(t).run("t", raises_timeout)
    assert not isinstance(e.value, ToolTimeoutError)
    assert len(t.sleeps) == 2  # retried like any other failure


# --- circuit breaker ---


async def test_breaker_opens_after_n_consecutive_failed_calls_and_fails_fast():
    t = FakeTime()
    r = runner(t, max_attempts=1, failure_threshold=3)
    tool = Flaky(failures=100)

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await r.run("t", tool)
    assert r.breaker("t").state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await r.run("t", tool)
    assert tool.attempts == 3  # the fourth call never ran


async def test_a_success_resets_the_failure_count():
    t = FakeTime()
    r = runner(t, max_attempts=1, failure_threshold=2)

    with pytest.raises(ConnectionError):
        await r.run("t", Flaky(failures=1))
    assert await r.run("t", Flaky(failures=0)) == "ok"
    with pytest.raises(ConnectionError):
        await r.run("t", Flaky(failures=1))

    assert r.breaker("t").state is CircuitState.CLOSED


async def test_a_call_that_succeeds_on_retry_is_not_a_failure():
    t = FakeTime()
    r = runner(t, max_attempts=3, failure_threshold=1)

    assert await r.run("t", Flaky(failures=2)) == "ok"
    assert r.breaker("t").state is CircuitState.CLOSED


async def test_timeouts_count_toward_the_breaker():
    r = ToolRunner(ResiliencePolicy(timeout_s=0.01, failure_threshold=2))
    for _ in range(2):
        with pytest.raises(ToolTimeoutError):
            await r.run("t", lambda: asyncio.sleep(10))

    assert r.breaker("t").state is CircuitState.OPEN


async def test_after_the_cooldown_one_trial_call_closes_it_on_success():
    t = FakeTime()
    r = runner(t, max_attempts=1, failure_threshold=1, cooldown_s=30)
    with pytest.raises(ConnectionError):
        await r.run("t", Flaky(failures=1))

    t.now += 29.9
    with pytest.raises(CircuitOpenError):
        await r.run("t", Flaky(failures=0))
    t.now += 0.1
    assert r.breaker("t").state is CircuitState.HALF_OPEN
    assert await r.run("t", Flaky(failures=0)) == "ok"
    assert r.breaker("t").state is CircuitState.CLOSED


async def test_a_failed_trial_call_reopens_it_for_a_full_cooldown():
    t = FakeTime()
    r = runner(t, max_attempts=1, failure_threshold=1, cooldown_s=30)
    with pytest.raises(ConnectionError):
        await r.run("t", Flaky(failures=1))

    t.now += 30
    with pytest.raises(ConnectionError):
        await r.run("t", Flaky(failures=1))
    assert r.breaker("t").state is CircuitState.OPEN
    t.now += 29.9
    with pytest.raises(CircuitOpenError):
        await r.run("t", Flaky(failures=0))


async def test_only_one_trial_call_runs_while_half_open():
    t = FakeTime()
    r = runner(t, max_attempts=1, failure_threshold=1, cooldown_s=30)
    with pytest.raises(ConnectionError):
        await r.run("t", Flaky(failures=1))
    t.now += 30

    release = asyncio.Event()

    async def trial():
        await release.wait()
        return "ok"

    first = asyncio.create_task(r.run("t", trial))
    await asyncio.sleep(0)
    with pytest.raises(CircuitOpenError):
        await r.run("t", Flaky(failures=0))
    release.set()
    assert await first == "ok"
    assert await r.run("t", Flaky(failures=0)) == "ok"


async def test_a_cancelled_trial_call_frees_the_trial_slot():
    t = FakeTime()
    r = runner(t, max_attempts=1, failure_threshold=1, cooldown_s=30)
    with pytest.raises(ConnectionError):
        await r.run("t", Flaky(failures=1))
    t.now += 30

    trial = asyncio.create_task(r.run("t", lambda: asyncio.sleep(10)))
    await asyncio.sleep(0)
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial

    assert await r.run("t", Flaky(failures=0)) == "ok"


async def test_each_tool_has_its_own_breaker():
    t = FakeTime()
    r = runner(t, max_attempts=1, failure_threshold=1)
    with pytest.raises(ConnectionError):
        await r.run("broken", Flaky(failures=1))

    assert await r.run("healthy", Flaky(failures=0)) == "ok"


def test_policy_from_env():
    policy = ResiliencePolicy.from_env({"TOOL_TIMEOUT_S": "2.5", "TOOL_BREAKER_FAILURE_THRESHOLD": "7"})

    assert policy.timeout_s == 2.5
    assert policy.failure_threshold == 7
    assert policy.max_attempts == ResiliencePolicy.max_attempts
