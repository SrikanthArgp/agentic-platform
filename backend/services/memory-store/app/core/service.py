"""`memory-store`'s two operations: record an event, get an alert's context.

- `record()`: Postgres first (the source of truth), then the Redis cache.
- `get_context()`: Redis; on a miss, rebuild from Postgres and refill
  Redis. If Redis itself fails, answer from Postgres alone: slower, never
  wrong.

Both name Redis keys by the app's `memory_namespace`, looked up from
`registry` (ADR-0018); Postgres rows are keyed by `app_id`.
"""

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from redis.exceptions import RedisError

from app.core.events import RETENTION_MS, Event, build_context
from app.db.postgres import EventStore
from app.redis.cache import MemoryCache
from proto_gen import memory_store_pb2
from registry_client import AppNotFoundError

logger = logging.getLogger(__name__)


class AppSource(Protocol):
    """`registry_client.RegistryClient`, or a fake in tests."""

    async def get_app(self, app_id: str) -> dict[str, Any]: ...


def now_ms() -> int:
    return int(time.time() * 1000)


class MemoryService:
    def __init__(
        self, store: EventStore, cache: MemoryCache, apps: AppSource, clock: Callable[[], int] = now_ms
    ):
        self._store = store
        self._cache = cache
        self._apps = apps
        self._clock = clock

    async def namespace(self, app_id: str) -> str:
        """Raises AppNotFoundError / RegistryUnavailableError."""
        app = await self._apps.get_app(app_id)
        return app.get("memory_namespace") or app_id

    async def get_context(self, app_id: str, alert_key: str) -> memory_store_pb2.GetContextResponse:
        namespace = await self.namespace(app_id)
        now = self._clock()
        cached = None
        try:
            cached = await self._cache.read(namespace, alert_key, now)
        except RedisError:
            logger.exception("redis read failed for %s/%s; answering from Postgres", app_id, alert_key)
            events, facts = await self._store.load(app_id, alert_key, now - RETENTION_MS)
            return build_context(app_id, alert_key, events, facts, now)

        if cached is None:
            events, facts = await self._store.load(app_id, alert_key, now - RETENTION_MS)
            try:
                await self._cache.fill(namespace, alert_key, events, facts)
            except RedisError:
                logger.exception("redis fill failed for %s/%s", app_id, alert_key)
        else:
            events, facts = cached
        return build_context(app_id, alert_key, events, facts, now)

    async def record(self, app_id: str, alert_key: str, event: Event) -> bool:
        """Store one event. Returns False for a duplicate.

        An unknown app is still stored (Postgres is keyed by app_id) but not
        cached; RegistryUnavailableError propagates so the caller retries.
        """
        try:
            namespace = await self.namespace(app_id)
        except AppNotFoundError:
            logger.warning("app %s not in registry; event %s stored without caching", app_id, event.ref_id)
            namespace = None

        inserted = await self._store.insert(app_id, alert_key, event)
        if namespace is not None:
            try:
                await self._cache.add(namespace, alert_key, event, self._clock())
            except RedisError:
                # The cache may now miss this event: drop the key so the
                # next read rebuilds it from Postgres.
                logger.exception("redis add failed for %s/%s; invalidating", app_id, alert_key)
                try:
                    await self._cache.invalidate(namespace, alert_key)
                except RedisError:
                    logger.exception("redis invalidate failed for %s/%s", app_id, alert_key)
        return inserted
