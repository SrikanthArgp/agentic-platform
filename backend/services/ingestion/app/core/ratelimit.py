"""Events per minute, per `app_id` and per alert source within it (docs/plan.md Day 14, ADR-0029).

Fixed one-minute windows, two Redis counters per event:

- `ratelimit:{app_id}:{window}`: every event posted for the app;
- `ratelimit:{app_id}:src:{source}:{window}`: those with that envelope
  `source`.

`window` is the Unix minute. An event is accepted only if both counters
are within their limits (the manifest's `rate_limit`, else
`RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_PER_SOURCE_PER_MINUTE`). Over either
-> `RateLimited` -> `429` with `Retry-After` = seconds to the next window.
Rejected events count too, so a client hammering through its `429`s
doesn't get a fresh allowance by retrying faster.

Each app has its own counters, so one app's flood never takes another's
allowance. `source` is whatever the sender wrote in the envelope (no
authentication, §13 T6): a sender can dodge its own per-source limit by
varying it, but not the app's limit.

Redis unreachable: the event is accepted and a warning logged. A rate
limit protects shared capacity; it isn't a correctness check, and
refusing every alert during a Redis outage would drop alerts at the door.
"""

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

WINDOW_S = 60
DEFAULT_PER_MINUTE = 600
DEFAULT_PER_SOURCE_PER_MINUTE = 300


@dataclass(frozen=True)
class Limits:
    """An app's manifest `rate_limit`; None fields fall back to the defaults."""

    per_minute: int | None = None
    per_source_per_minute: int | None = None

    @classmethod
    def from_manifest(cls, rate_limit: Any) -> "Limits":
        if not isinstance(rate_limit, dict):
            return cls()
        return cls(rate_limit.get("per_minute"), rate_limit.get("per_source_per_minute"))


class RateLimited(Exception):
    def __init__(self, app_id: str, scope: str, limit: int, retry_after_s: int, source: str):
        what = f"app '{app_id}'" if scope == "per_minute" else f"source '{source}' of app '{app_id}'"
        super().__init__(f"rate limit exceeded: {what} is over {limit} events per minute; retry in {retry_after_s}s.")
        self.scope = scope
        self.limit = limit
        self.retry_after_s = retry_after_s


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        default_per_minute: int = DEFAULT_PER_MINUTE,
        default_per_source_per_minute: int = DEFAULT_PER_SOURCE_PER_MINUTE,
        clock: Callable[[], float] = time.time,
    ):
        self._redis = redis
        self._default_per_minute = default_per_minute
        self._default_per_source = default_per_source_per_minute
        self._clock = clock

    async def hit(self, app_id: str, source: str, limits: Limits) -> None:
        """Count one event; raises `RateLimited` if it's over either limit."""
        now = self._clock()
        window = int(now // WINDOW_S)
        app_key = f"ratelimit:{app_id}:{window}"
        source_key = f"ratelimit:{app_id}:src:{source}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(app_key)
                pipe.incr(source_key)
                # Two windows' worth: a key outlives its minute, never by much.
                pipe.expire(app_key, 2 * WINDOW_S)
                pipe.expire(source_key, 2 * WINDOW_S)
                app_count, source_count, *_ = await pipe.execute()
        except RedisError as e:
            logger.warning("rate limit not checked for app %s (Redis unavailable: %s); event accepted", app_id, e)
            return

        retry_after = max(1, math.ceil((window + 1) * WINDOW_S - now))
        per_minute = limits.per_minute or self._default_per_minute
        per_source = limits.per_source_per_minute or self._default_per_source
        if app_count > per_minute:
            raise RateLimited(app_id, "per_minute", per_minute, retry_after, source)
        if source_count > per_source:
            raise RateLimited(app_id, "per_source_per_minute", per_source, retry_after, source)
