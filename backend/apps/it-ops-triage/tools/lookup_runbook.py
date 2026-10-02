"""`lookup_runbook`: the runbook entry for an IT-ops alert type.

App-owned, read-only, fixture-backed (ADR-0004, ADR-0009). `tool-gateway`
imports this module by file path at startup and registers everything in
`TOOLS` (docs/ARCHITECTURE.md §12). This module must not import platform
code; the only contract with `tool-gateway` is the shape of `TOOLS`.

The fixture is human-curated and changes only through git (no tool or agent
writes it). The output shape is meant to survive swapping the fixture for a
real runbook source (a wiki, PagerDuty, ...) later.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FIXTURE_PATH = Path(__file__).with_name("runbooks.json")


class LookupRunbookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_type: str = Field(
        description="The alert's type, e.g. 'disk_full' or 'high_cpu'.",
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )


class RunbookEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runbook_id: str = Field(description="Stable id to cite in reasons, e.g. 'RB-001'.")
    alert_type: str
    title: str
    description: str
    likely_causes: list[str]
    diagnostic_checks: list[str] = Field(
        description="Checks a human performs. Guidance only; nothing executes them."
    )
    suggested_action: Literal["AUTO_RESOLVE", "ESCALATE", "SUPPRESS"] = Field(
        description="Advisory default. The agent decides; escalate_when guardrails still apply."
    )
    action_conditions: str = Field(
        description="When suggested_action applies and when it does not."
    )
    owner_team: str
    severity_hint: Literal["low", "medium", "high", "critical"]


class LookupRunbookOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    alert_type: str
    runbook: RunbookEntry | None = None
    message: str | None = None


def _load_runbooks(path: Path) -> dict[str, RunbookEntry]:
    entries = [RunbookEntry.model_validate(e) for e in json.loads(path.read_text())]
    runbooks = {e.alert_type: e for e in entries}
    if len(runbooks) != len(entries):
        raise ValueError(f"{path}: duplicate alert_type")
    return runbooks


RUNBOOKS = _load_runbooks(FIXTURE_PATH)


def lookup_runbook(args: LookupRunbookInput) -> LookupRunbookOutput:
    entry = RUNBOOKS.get(args.alert_type)
    if entry is None:
        return LookupRunbookOutput(
            found=False,
            alert_type=args.alert_type,
            message=f"No runbook for alert_type '{args.alert_type}'.",
        )
    return LookupRunbookOutput(found=True, alert_type=args.alert_type, runbook=entry)


TOOLS = {
    "lookup_runbook": {
        "version": "1.0.0",
        "description": (
            "Look up the runbook for an IT-ops alert type: what it means, likely "
            "causes, and the suggested triage action. Returns found=false when no "
            "runbook exists for that alert type."
        ),
        "input_model": LookupRunbookInput,
        "output_model": LookupRunbookOutput,
        "handler": lookup_runbook,
    },
}
