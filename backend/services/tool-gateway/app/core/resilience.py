"""Timeout, retry/backoff and a circuit breaker around one tool's handler
(docs/plan.md Day 12, ADR-0025; adapted from
`ai_microservice_patterns/07_circuit_breaker_retry_backoff`).

`ToolRunner.run()` is one tool call, after its arguments have been
validated:

- **Deadline**: the whole call, every attempt and backoff included, gets
  `timeout_s`. Each attempt gets what's left of it, so a call never takes
  longer than `timeout_s` and a timed-out attempt is never retried (there's
  no time left). Raises `ToolTimeoutError`.
- **Retry with backoff**: an attempt that raises is retried, up to
  `max_attempts` in all, after `base_delay_s * 2**n` (capped at
  `max_delay_s`, jittered by 0.5x-1.5x). Safe because every tool is a
  read-only lookup (ADR-0004). A retry that wouldn't fit before the
  deadline isn't made; the last error is raised.
- **Circuit breaker**, one per tool: `failure_threshold` consecutive
  failed *calls* (after their retries; timeouts included) open it, and for
  `cooldown_s` every call fails fast with `CircuitOpenError` without
  running the handler. After the cooldown one trial call runs (half-open)
  while the others keep failing fast; it closes the breaker on success and
  reopens it on failure.

Breakers live in memory, per `tool-gateway` replica: each replica learns
on its own that a tool is broken, within `failure_threshold` calls.

`ToolSpec.run` runs a sync handler in a worker thread, so a slow one
can't block the event loop. A thread can't be cancelled: on a timeout the
caller gets its answer on time, but the thread runs on until the handler
returns. The breaker bounds how many such threads a hung tool can tie up.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ToolTimeoutError(TimeoutError):
    pass


class CircuitOpenError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResiliencePolicy:
    timeout_s: float = 5.0
    max_attempts: int = 3
    base_delay_s: float = 0.1
    max_delay_s: float = 1.0
    failure_threshold: int = 5
    cooldown_s: float = 30.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "ResiliencePolicy":
        return cls(
            timeout_s=float(env.get("TOOL_TIMEOUT_S", cls.timeout_s)),
            max_attempts=int(env.get("TOOL_MAX_ATTEMPTS", cls.max_attempts)),
            base_delay_s=float(env.get("TOOL_RETRY_BASE_DELAY_S", cls.base_delay_s)),
            max_delay_s=float(env.get("TOOL_RETRY_MAX_DELAY_S", cls.max_delay_s)),
            failure_threshold=int(env.get("TOOL_BREAKER_FAILURE_THRESHOLD", cls.failure_threshold)),
            cooldown_s=float(env.get("TOOL_BREAKER_COOLDOWN_S", cls.cooldown_s)),
        )


class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int, cooldown_s: float, clock: Callable[[], float]):
        self.name = name
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._trial_running = False

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self._cooldown_s:
            return CircuitState.HALF_OPEN
        return self._state

    def before_call(self) -> None:
        """Raises `CircuitOpenError` if this call must fail fast."""
        state = self.state
        if state is CircuitState.CLOSED:
            return
        if state is CircuitState.HALF_OPEN and not self._trial_running:
            self._state = CircuitState.HALF_OPEN
            self._trial_running = True
            logger.info("circuit for tool %s half-open: running one trial call", self.name)
            return
        raise CircuitOpenError(f"circuit open for tool '{self.name}'")

    def on_success(self) -> None:
        if self._state is not CircuitState.CLOSED:
            logger.info("circuit for tool %s closed", self.name)
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._trial_running = False

    def on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN or self._consecutive_failures >= self._failure_threshold:
            if self._state is not CircuitState.OPEN:
                logger.warning(
                    "circuit for tool %s opened after %d consecutive failed calls",
                    self.name, self._consecutive_failures,
                )
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
        self._trial_running = False

    def on_cancel(self) -> None:
        """The caller went away: says nothing about the tool, but frees the trial slot."""
        self._trial_running = False


class ToolRunner:
    def __init__(
        self,
        policy: ResiliencePolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.5, 1.5),
    ):
        """`clock`, `sleep` and `jitter` are for tests."""
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, tool_id: str) -> CircuitBreaker:
        if (b := self._breakers.get(tool_id)) is None:
            p = self._policy
            b = self._breakers[tool_id] = CircuitBreaker(tool_id, p.failure_threshold, p.cooldown_s, self._clock)
        return b

    async def run(self, tool_id: str, attempt: Callable[[], Awaitable[Any]]) -> Any:
        """Run `attempt` (one call of the tool) under the policy."""
        breaker = self.breaker(tool_id)
        breaker.before_call()
        try:
            result = await self._with_retries(tool_id, attempt)
        except asyncio.CancelledError:
            breaker.on_cancel()
            raise
        except Exception:
            breaker.on_failure()
            raise
        breaker.on_success()
        return result

    def delay_s(self, attempt: int) -> float:
        """Backoff before retry number `attempt` (1 = the first retry)."""
        p = self._policy
        return min(p.base_delay_s * 2 ** (attempt - 1), p.max_delay_s) * self._jitter()

    async def _with_retries(self, tool_id: str, attempt: Callable[[], Awaitable[Any]]) -> Any:
        deadline = self._clock() + self._policy.timeout_s
        n = 1
        while True:
            timeout = asyncio.timeout(deadline - self._clock())
            try:
                async with timeout:
                    return await attempt()
            except Exception as e:
                # Only our deadline is a timeout; a handler's own TimeoutError
                # is an ordinary failure.
                if timeout.expired():
                    raise ToolTimeoutError(
                        f"tool '{tool_id}' timed out after {self._policy.timeout_s:g}s (attempt {n})"
                    ) from None
                if n >= self._policy.max_attempts:
                    raise
                delay = self.delay_s(n)
                if self._clock() + delay >= deadline:
                    raise
                logger.warning("tool %s attempt %d failed (%r); retrying in %.3fs", tool_id, n, e, delay)
                await self._sleep(delay)
                n += 1
