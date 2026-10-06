"""The `POST /apps/{app_id}/events` request and responses.

The request is the platform-generic envelope (the named `RunAgentRequest`
fields) plus `payload`, the app-specific event validated against the app's
`event_schema_ref` (ADR-0007). `app_id` comes from the URL; `alert_id` and
`alert_key` are set by ingestion. A body that tries to set any of them is a
`422`.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class AlertIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=128, description="The system that raised the alert.")
    severity: str = Field(
        pattern=r"^[a-z][a-z0-9_-]{0,31}$", description="Source's severity label, e.g. 'warning', 'critical'."
    )
    message: str = Field(min_length=1, max_length=2000, description="Human-readable alert text.")
    timestamp: AwareDatetime | None = Field(
        default=None, description="When the alert fired (ISO 8601 with a UTC offset). Defaults to when ingestion received it."
    )
    payload: dict[str, Any] = Field(description="The app-specific event; validated against the app's schema.")


class AlertAccepted(BaseModel):
    alert_id: str
    app_id: str
    alert_key: str
    status: Literal["accepted"] = "accepted"


class AlertStatus(BaseModel):
    """Debug view of one alert (Day 4 only; review-console replaced it on Day 9, removed on Day 10)."""

    alert_id: str
    app_id: str
    alert_key: str
    status: Literal["pending", "decided"]
    accepted_at: datetime
    decided_at: datetime | None = None
    latency_ms: int | None = None
    decision: str | None = None
    agent_id: str | None = None
    reasons: list[str] = []
    tool_calls: list[dict[str, str]] = []
