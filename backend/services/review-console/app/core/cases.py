"""Cases: what an escalated alert becomes, and its one-way verdict (plan.md Day 9).

Only `ESCALATE` decisions become cases (docs/ARCHITECTURE.md §4 step 4);
`AUTO_RESOLVE`/`SUPPRESS` are high-volume and need no human. A case moves
`OPEN` -> `RESOLVED` exactly once, when an analyst records a verdict; a
second verdict is rejected, never applied over the first.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, Field, field_validator

from proto_gen import agent_pb2

# What the API accepts for free text that later reaches prompts (§13 T2).
MAX_NOTES_CHARS = 4000


class CaseStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class Verdict(StrEnum):
    """Also the `verdict.recorded` values `memory-store` counts (Day 10, §6)."""

    CONFIRMED_INCIDENT = "CONFIRMED_INCIDENT"
    CONFIRMED_NOISE = "CONFIRMED_NOISE"


class ToolCallSummary(BaseModel):
    tool_name: str
    result_summary: str
    # Which agent made the call: the entry agent or a callable (ADR-0023).
    # Empty on a case from before it was recorded.
    agent_id: str = ""


class AlertSummary(BaseModel):
    """The alert a case is about, as orchestrator received it (ADR-0023).
    Untrusted: the payload comes from the alert's source (§13)."""

    source: str
    severity: str
    message: str
    fired_at: datetime | None
    # App-specific fields; numbers come back as floats (protobuf Struct).
    payload: dict[str, Any]


class NewCase(BaseModel):
    """A case built from one `alert.decided` message, before it has an id."""

    app_id: str
    alert_id: str
    alert_key: str
    agent_id: str
    decision: str
    confidence: float
    reasons: list[str]
    tool_calls: list[ToolCallSummary]
    # None when the decision didn't carry the alert (published before ADR-0023).
    alert: AlertSummary | None = None

    @field_validator("confidence")
    @classmethod
    def _round(cls, v: float) -> float:
        # A float32 on the wire and REAL in Postgres: 0.55 reads back as 0.5500000119.
        return round(v, 4)


class Case(NewCase):
    id: int
    status: CaseStatus
    verdict: Verdict | None = None
    verdict_by: str | None = None
    resolution_notes: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class VerdictIn(BaseModel):
    verdict: Verdict
    # Self-asserted until analyst authn (phase two, §13 T7).
    verdict_by: str = Field(min_length=1, max_length=200)
    # What was actually done to fix it; stays on the case, never in Kafka
    # (ADR-0011).
    resolution_notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)

    @field_validator("verdict_by")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("verdict_by must not be blank")
        return v.strip()

    @field_validator("resolution_notes")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class CaseNotFoundError(LookupError):
    pass


class CaseAlreadyResolvedError(RuntimeError):
    def __init__(self, case: Case):
        super().__init__(f"case {case.id} was already resolved as {case.verdict} by {case.verdict_by}")
        self.case = case


def case_from_decision(response: agent_pb2.RunAgentResponse) -> NewCase | None:
    """The persistence filter: a case for an `ESCALATE` decision, else None."""
    if response.decision != agent_pb2.ESCALATE:
        return None
    return NewCase(
        app_id=response.app_id,
        alert_id=response.alert_id,
        alert_key=response.alert_key,
        agent_id=response.agent_id,
        decision=agent_pb2.Decision.Name(response.decision),
        confidence=response.confidence,
        reasons=list(response.reasons),
        tool_calls=[
            ToolCallSummary(tool_name=c.tool_name, result_summary=c.result_summary, agent_id=c.agent_id)
            for c in response.tool_calls
        ],
        alert=_alert(response.alert) if response.HasField("alert") else None,
    )


def _alert(request: agent_pb2.RunAgentRequest) -> AlertSummary:
    ms = request.timestamp_unix_ms
    return AlertSummary(
        source=request.source,
        severity=request.severity,
        message=request.message,
        fired_at=datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None,
        payload=MessageToDict(request.payload),
    )


def verdict_event(case: Case) -> dict:
    """The `verdict.recorded` body (docs/ARCHITECTURE.md §9): counts and
    flags only, so `resolution_notes` is deliberately absent."""
    assert case.verdict is not None and case.resolved_at is not None
    return {
        "app_id": case.app_id,
        "case_id": case.id,
        "alert_key": case.alert_key,
        "verdict": case.verdict.value,
        "verdict_by": case.verdict_by,
        "recorded_at": case.resolved_at.isoformat(),
    }
