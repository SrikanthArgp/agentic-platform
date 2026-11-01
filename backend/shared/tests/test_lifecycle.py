import signal

import pytest

from lifecycle import Lifecycle

pytestmark = pytest.mark.anyio


@pytest.fixture
def restore_sigterm():
    original = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, original)


def test_sigterm_marks_draining_then_calls_the_previous_handler(restore_sigterm):
    calls = []
    signal.signal(signal.SIGTERM, lambda sig, frame: calls.append(sig))
    life = Lifecycle("svc")
    life.mark_started()
    life.install_sigterm_hook()

    signal.raise_signal(signal.SIGTERM)

    assert life.draining is True
    assert calls == [signal.SIGTERM]


def test_no_previous_handler_is_left_alone(restore_sigterm):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    life = Lifecycle("svc")
    life.install_sigterm_hook()

    assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN


async def test_not_ready_until_started_and_again_once_draining():
    life = Lifecycle("svc")
    assert await life.not_ready_reasons() == ["starting"]

    life.mark_started()
    assert await life.not_ready_reasons() == []

    life.begin_drain()
    assert await life.not_ready_reasons() == ["draining"]


async def test_failing_or_raising_checks_are_reported_by_name():
    life = Lifecycle("svc")
    life.mark_started()

    async def ok():
        return None

    async def down():
        return "connection refused"

    async def broken():
        raise RuntimeError("bug")

    reasons = await life.not_ready_reasons({"a": ok, "postgres": down, "kafka": broken})
    assert reasons == ["postgres: connection refused", "kafka: RuntimeError: bug"]


async def test_draining_skips_dependency_checks():
    life = Lifecycle("svc")
    life.mark_started()
    life.begin_drain()
    called = []

    async def check():
        called.append(1)
        return None

    assert await life.not_ready_reasons({"x": check}) == ["draining"]
    assert called == []
