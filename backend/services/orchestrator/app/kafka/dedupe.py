"""Skip an alert that was already decided (Day 16, docs/ARCHITECTURE.md §10).

`alert.received` is delivered at least once: the `outbox-relay` re-publishes
a row if it crashes between the send and marking it `SENT` (ADR-0030), and
this consumer re-reads a message if it crashes before committing. A Redis
marker `seen:{app_id}:{alert_id}` (TTL'd, shared by every replica) says
"this alert's decision is already published".

The marker is set only *after* `alert.decided` is published, so a crash
mid-run re-runs the alert instead of losing it. Redis down → the check says
"not seen" and the alert runs: a possible duplicate decision is better than
a dropped alert, and `review-console`/`memory-store` dedupe on their side.
"""

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 24 * 3600


def seen_key(app_id: str, alert_id: str) -> str:
    return f"seen:{app_id}:{alert_id}"


class SeenAlerts:
    def __init__(self, redis: Redis, ttl_s: int = DEFAULT_TTL_S):
        self._redis = redis
        self._ttl_s = ttl_s

    async def seen(self, app_id: str, alert_id: str) -> bool:
        try:
            return bool(await self._redis.exists(seen_key(app_id, alert_id)))
        except (RedisError, OSError) as e:
            logger.warning("dedupe check skipped for %s/%s (Redis: %s); running it", app_id, alert_id, e)
            return False

    async def mark(self, app_id: str, alert_id: str) -> None:
        try:
            await self._redis.set(seen_key(app_id, alert_id), "1", ex=self._ttl_s)
        except (RedisError, OSError) as e:
            logger.warning("couldn't mark %s/%s as decided (Redis: %s)", app_id, alert_id, e)
