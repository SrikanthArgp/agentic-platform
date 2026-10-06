"""Memory events and the rolling-window maths (docs/adr/0017).

Memory is an event log: every decision and every analyst verdict for an
`alert_key` is one `Event`. `GetContext` counts are computed by
`build_context()` from the last 7 days of events plus two all-time
facts, whether those came from Redis or from Postgres, so the cache and the
rebuild can't disagree.

Windows are `(now - w, now]` in milliseconds: an event exactly `w` old has
left the window. Events stamped slightly after `now` (clock skew between
the Kafka producer and this service) still count.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from proto_gen import agent_pb2, memory_store_pb2

HOUR_MS = 3_600_000
WINDOWS_MS = {"1h": HOUR_MS, "24h": 24 * HOUR_MS, "7d": 7 * 24 * HOUR_MS}
# The longest window: older events are trimmed from the cache.
RETENTION_MS = WINDOWS_MS["7d"]

DECISION = "decision"
VERDICT = "verdict"
# review-console's verdict.recorded values (docs/ARCHITECTURE.md §9).
CONFIRMED_INCIDENT = f"{VERDICT}:CONFIRMED_INCIDENT"
CONFIRMED_NOISE = f"{VERDICT}:CONFIRMED_NOISE"
VERDICT_KINDS = (CONFIRMED_INCIDENT, CONFIRMED_NOISE)


@dataclass(frozen=True)
class Event:
    # "decision:<Decision name>" or "verdict:<verdict>".
    kind: str
    # What the event is about: the alert_id for a decision, the case_id for
    # a verdict. (app_id, family, ref_id) is unique: a redelivered event
    # counts once.
    ref_id: str
    at_ms: int

    @property
    def family(self) -> str:
        return self.kind.split(":", 1)[0]


@dataclass(frozen=True)
class Facts:
    """All-time facts about an alert_key, kept beyond the 7-day window."""

    seen: bool = False
    confirmed_incident: bool = False

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> "Facts":
        seen = confirmed = False
        for e in events:
            seen = seen or e.family == DECISION
            confirmed = confirmed or e.kind == CONFIRMED_INCIDENT
        return cls(seen=seen, confirmed_incident=confirmed)


def decision_event(response: agent_pb2.RunAgentResponse, at_ms: int) -> Event:
    return Event(kind=f"{DECISION}:{agent_pb2.Decision.Name(response.decision)}", ref_id=response.alert_id, at_ms=at_ms)


def verdict_event(case_id: str, verdict: str, at_ms: int) -> Event:
    """An analyst verdict from `verdict.recorded`; one per case, ever."""
    kind = f"{VERDICT}:{verdict}"
    if kind not in VERDICT_KINDS:
        raise ValueError(f"unknown verdict {verdict!r}")
    return Event(kind=kind, ref_id=case_id, at_ms=at_ms)


def aggregate(events: Iterable[Event], now_ms: int, window_ms: int) -> memory_store_pb2.ContextAggregate:
    agg = memory_store_pb2.ContextAggregate()
    since = now_ms - window_ms
    for e in events:
        if e.at_ms <= since:
            continue
        if e.family == DECISION:
            agg.alert_count += 1
            if e.kind == f"{DECISION}:ESCALATE":
                agg.escalation_count += 1
            elif e.kind == f"{DECISION}:SUPPRESS":
                agg.suppression_count += 1
        elif e.kind == CONFIRMED_INCIDENT:
            agg.confirmed_incident_count += 1
        elif e.kind == CONFIRMED_NOISE:
            agg.confirmed_noise_count += 1
    return agg


def build_context(
    app_id: str, alert_key: str, events: list[Event], facts: Facts, now_ms: int
) -> memory_store_pb2.GetContextResponse:
    return memory_store_pb2.GetContextResponse(
        app_id=app_id,
        alert_key=alert_key,
        window_1h=aggregate(events, now_ms, WINDOWS_MS["1h"]),
        window_24h=aggregate(events, now_ms, WINDOWS_MS["24h"]),
        window_7d=aggregate(events, now_ms, WINDOWS_MS["7d"]),
        is_novel_alert=not facts.seen,
        has_confirmed_incident_history=facts.confirmed_incident,
    )
