"""Readiness and graceful shutdown for a platform service (docs/plan.md Day 19, ADR-0014).

How Kubernetes stops a pod: it takes the pod out of its Service's endpoints
while the container's `preStop` hook sleeps, then sends SIGTERM, then
SIGKILL after `terminationGracePeriodSeconds`. uvicorn handles SIGTERM by
closing its listening socket, letting in-flight requests finish, and then
running the app's lifespan shutdown, where each service drains its own
background work (`orchestrator`: the current Kafka message).

`Lifecycle` is the part uvicorn doesn't know about: whether this process
is ready (startup finished) or draining (SIGTERM seen). `/readyz` reports
503 in either not-ready state, so a pod is never routed to before it can
serve, nor after it has started to stop. `install_sigterm_hook()` must
run once uvicorn has installed its own handler (i.e. in the lifespan
startup); it marks the process draining and then calls uvicorn's handler,
so shutdown proceeds exactly as before. The same applies to `docker stop`
under Compose.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from types import FrameType

logger = logging.getLogger(__name__)

# One readiness check: a name and an async callable that returns why the
# dependency isn't usable, or None when it is.
Check = Callable[[], Awaitable[str | None]]


class Lifecycle:
    def __init__(self, service: str):
        self.service = service
        self.started = False
        self.draining = False

    def mark_started(self) -> None:
        self.started = True

    def begin_drain(self) -> None:
        if not self.draining:
            logger.info("%s draining: /readyz now fails, in-flight work finishes before exit", self.service)
        self.draining = True

    def install_sigterm_hook(self) -> None:
        """Wrap the current SIGTERM handler (uvicorn's) so draining starts
        first. A non-callable previous handler (SIG_DFL/SIG_IGN) is kept as
        it was: no exit is invented where there was none."""
        previous = signal.getsignal(signal.SIGTERM)

        def on_sigterm(sig: int, frame: FrameType | None) -> None:
            self.begin_drain()
            if callable(previous):
                previous(sig, frame)

        if callable(previous):
            signal.signal(signal.SIGTERM, on_sigterm)
        else:
            logger.warning("no SIGTERM handler to wrap (%r); draining starts only at lifespan shutdown", previous)

    async def not_ready_reasons(self, checks: Mapping[str, Check] | None = None) -> list[str]:
        """Empty when ready. Draining and not-yet-started short-circuit the
        dependency checks: a pod on its way out shouldn't probe anything."""
        if self.draining:
            return ["draining"]
        if not self.started:
            return ["starting"]
        reasons = []
        for name, check in (checks or {}).items():
            try:
                why = await check()
            except Exception as e:  # a broken check means not ready, never a 500
                why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            if why:
                reasons.append(f"{name}: {why}")
        return reasons
