"""The `verdict_outbox` row a verdict writes (Day 15, ADR-0024, ADR-0030).

`review-console` never publishes `verdict.recorded` itself. The verdict's
transaction resolves the case *and* inserts one `verdict_outbox` row, so
there's a resolved case with its event or neither. The `outbox-relay`
worker publishes the row to Kafka with its `created_at` as the message
timestamp, which is what `memory-store` times a verdict by.
"""

from dataclasses import dataclass

from opentelemetry.propagate import inject

VERDICT_RECORDED = "verdict.recorded"


@dataclass(frozen=True)
class VerdictOutboxRow:
    app_id: str
    case_id: int
    topic: str
    kafka_key: str
    payload: bytes
    headers: dict[str, str]


class StoreUnavailableError(RuntimeError):
    """Postgres didn't take the verdict; nothing was committed."""


def message_key(app_id: str, alert_key: str) -> str:
    """Every topic's key (docs/ARCHITECTURE.md §8)."""
    return f"{app_id}:{alert_key}"


def trace_headers() -> dict[str, str]:
    """W3C trace context of the current span (empty when there's none)."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier
