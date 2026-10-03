"""Build the `alert.received` message: `alert_key`, envelope, Kafka key.

`alert_key` is built here, from the payload fields the app's manifest names
in `alert_key_fields`, joined with ":" in that order, never by the agent,
so the same event always lands on the same memory key and partition
(ADR-0007, docs/ARCHITECTURE.md §8).
"""

from datetime import datetime
from typing import Any

from app.models.alerts import AlertIn
from proto_gen import agent_pb2


class AlertKeyError(ValueError):
    pass


def build_alert_key(payload: dict[str, Any], fields: tuple[str, ...]) -> str:
    parts = []
    for field in fields:
        value = payload.get(field)
        if isinstance(value, bool):
            part = str(value).lower()
        elif isinstance(value, (str, int, float)):
            part = str(value)
        else:
            raise AlertKeyError(f"payload.{field} is required to build alert_key and must be a string or number.")
        if not part:
            raise AlertKeyError(f"payload.{field} is required to build alert_key and must not be empty.")
        parts.append(part)
    return ":".join(parts)


def message_key(app_id: str, alert_key: str) -> bytes:
    """Every message on every topic is keyed `{app_id}:{alert_key}` (ADR-0002)."""
    return f"{app_id}:{alert_key}".encode()


def build_request(
    *, app_id: str, alert_id: str, alert_key: str, alert: AlertIn, received_at: datetime
) -> agent_pb2.RunAgentRequest:
    fired_at = alert.timestamp or received_at
    request = agent_pb2.RunAgentRequest(
        app_id=app_id,
        alert_id=alert_id,
        alert_key=alert_key,
        source=alert.source,
        severity=alert.severity,
        message=alert.message,
        timestamp_unix_ms=int(fired_at.timestamp() * 1000),
    )
    # Struct stores every number as a double (ADR-0007).
    request.payload.update(alert.payload)
    return request
