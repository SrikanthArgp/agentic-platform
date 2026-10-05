"""The Redis cache of one `alert_key`'s memory (docs/adr/0017, ARCHITECTURE §8).

Two keys per `{memory_namespace}:{alert_key}`:

- `mem:{ns}:{key}:events`: sorted set of the last 7 days of events, member
  `{kind}|{ref_id}`, score = event time in ms. Trimmed on every write and
  read.
- `mem:{ns}:{key}:facts`: hash of all-time facts (`seen`,
  `confirmed_incident`, each present only when true) and the `loaded`
  marker, which says the sorted set holds *every* recent event.

Without `loaded` the key is a miss and is rebuilt from Postgres, even if a
writer has already added a few events to it. Every operation only adds
(ZADD, and HSET of true facts), so a rebuild racing a write can't drop the
write: the result is the union. Both keys get `ttl_s` on every touch, so
idle alert keys expire and are rebuilt on demand.
"""

from redis.asyncio import Redis

from app.core.events import CONFIRMED_INCIDENT, DECISION, RETENTION_MS, Event, Facts

DEFAULT_TTL_S = 8 * 24 * 3600  # the 7-day window plus a day
_LOADED = "loaded"


def _keys(namespace: str, alert_key: str) -> tuple[str, str]:
    base = f"mem:{namespace}:{alert_key}"
    return f"{base}:events", f"{base}:facts"


def _member(event: Event) -> str:
    return f"{event.kind}|{event.ref_id}"


def _true_facts(facts: Facts) -> dict[str, str]:
    fields = {}
    if facts.seen:
        fields["seen"] = "1"
    if facts.confirmed_incident:
        fields["confirmed_incident"] = "1"
    return fields


class MemoryCache:
    def __init__(self, redis: Redis, ttl_s: int = DEFAULT_TTL_S):
        self._redis = redis
        self._ttl_s = ttl_s

    async def read(self, namespace: str, alert_key: str, now_ms: int) -> tuple[list[Event], Facts] | None:
        """The cached events and facts, or None on a miss."""
        events_key, facts_key = _keys(namespace, alert_key)
        cutoff = now_ms - RETENTION_MS
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(events_key, "-inf", cutoff)
            pipe.zrangebyscore(events_key, f"({cutoff}", "+inf", withscores=True)
            pipe.hgetall(facts_key)
            _, members, facts = await pipe.execute()
        if facts.get(_LOADED) != "1":
            return None
        events = []
        for member, score in members:
            kind, _, ref_id = member.partition("|")
            events.append(Event(kind=kind, ref_id=ref_id, at_ms=int(score)))
        return events, Facts(seen=facts.get("seen") == "1", confirmed_incident=facts.get("confirmed_incident") == "1")

    async def fill(self, namespace: str, alert_key: str, events: list[Event], facts: Facts) -> None:
        """Store a rebuild from Postgres and mark the key loaded."""
        events_key, facts_key = _keys(namespace, alert_key)
        async with self._redis.pipeline(transaction=True) as pipe:
            if events:
                pipe.zadd(events_key, {_member(e): e.at_ms for e in events})
            pipe.hset(facts_key, mapping={**_true_facts(facts), _LOADED: "1"})
            pipe.expire(events_key, self._ttl_s)
            pipe.expire(facts_key, self._ttl_s)
            await pipe.execute()

    async def add(self, namespace: str, alert_key: str, event: Event, now_ms: int) -> None:
        """Add one new event (already stored in Postgres)."""
        events_key, facts_key = _keys(namespace, alert_key)
        facts = Facts(seen=event.family == DECISION, confirmed_incident=event.kind == CONFIRMED_INCIDENT)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(events_key, {_member(event): event.at_ms})
            pipe.zremrangebyscore(events_key, "-inf", now_ms - RETENTION_MS)
            if fields := _true_facts(facts):
                pipe.hset(facts_key, mapping=fields)
            pipe.expire(events_key, self._ttl_s)
            pipe.expire(facts_key, self._ttl_s)
            await pipe.execute()

    async def invalidate(self, namespace: str, alert_key: str) -> None:
        """Forget the key, so the next read rebuilds it from Postgres."""
        await self._redis.delete(*_keys(namespace, alert_key))
