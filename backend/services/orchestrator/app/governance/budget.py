"""Daily token budgets per `app_id` + `agent_id` (docs/plan.md Day 14, ADR-0029).

One Redis hash per agent per UTC day, `budget:{app_id}:{agent_id}:{YYYY-MM-DD}`
(docs/ARCHITECTURE.md §8), with `input_tokens` and `output_tokens` kept
apart so Day 24 can price them. The budget is in tokens, input plus
output: `daily_tokens` from the agent's manifest entry, else
`DAILY_TOKEN_BUDGET`.

- `check()` before a run: an agent that has already used its day's budget
  isn't run (`BudgetExceededError`). An entry agent's alert is then "not
  evaluated" and escalates; a callable doesn't contribute.
- `record()` after a run adds what it used. A run's usage is only known
  once it ends, so the run that crosses the line finishes, and concurrent
  runs that all passed the check can each overshoot by one run.
- A new UTC day is a new key, so the budget resets at midnight UTC; old
  keys expire after `ttl_s`.

Redis unreachable: `check()` raises `BudgetUnavailableError` and the
caller runs anyway, saying so in a reason (a budget is a spending limit,
not a correctness check; escalating every alert during a Redis outage
would bury analysts). `record()` logs and drops the usage.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

DEFAULT_DAILY_TOKENS = 1_000_000
DEFAULT_TTL_S = 2 * 24 * 3600


class BudgetExceededError(RuntimeError):
    def __init__(self, app_id: str, agent_id: str, used: int, limit: int):
        super().__init__(
            f"budget exceeded: agent '{agent_id}' of app '{app_id}' has used {used:,} of its "
            f"{limit:,} daily tokens (resets at 00:00 UTC)"
        )
        self.used = used
        self.limit = limit


class BudgetUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class TokenBudget:
    def __init__(
        self,
        redis: Redis,
        *,
        default_daily_tokens: int = DEFAULT_DAILY_TOKENS,
        ttl_s: int = DEFAULT_TTL_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._redis = redis
        self._default = default_daily_tokens
        self._ttl_s = ttl_s
        self._clock = clock

    def key(self, app_id: str, agent_id: str) -> str:
        return f"budget:{app_id}:{agent_id}:{self._clock().astimezone(UTC).date().isoformat()}"

    def limit(self, daily_tokens: int | None) -> int:
        return self._default if daily_tokens is None else daily_tokens

    async def used(self, app_id: str, agent_id: str) -> Usage:
        try:
            values = await self._redis.hmget(self.key(app_id, agent_id), ["input_tokens", "output_tokens"])
        except RedisError as e:
            raise BudgetUnavailableError(f"Redis unavailable ({type(e).__name__})") from e
        return Usage(*(int(v or 0) for v in values))

    async def check(self, app_id: str, agent_id: str, daily_tokens: int | None) -> None:
        """Raises `BudgetExceededError` if the agent has used its day's
        budget, `BudgetUnavailableError` if that can't be known."""
        limit = self.limit(daily_tokens)
        used = (await self.used(app_id, agent_id)).total
        if used >= limit:
            raise BudgetExceededError(app_id, agent_id, used, limit)

    async def record(self, app_id: str, agent_id: str, usage: Usage) -> None:
        if usage.total == 0:
            return
        key = self.key(app_id, agent_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hincrby(key, "input_tokens", usage.input_tokens)
                pipe.hincrby(key, "output_tokens", usage.output_tokens)
                pipe.expire(key, self._ttl_s)
                await pipe.execute()
        except RedisError as e:
            logger.warning("token usage not recorded for %s/%s (%d tokens): %s", app_id, agent_id, usage.total, e)
