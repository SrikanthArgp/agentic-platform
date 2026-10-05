"""`recent-changes-lookup`: deploys, config and infra changes to a service or
host within a time window (docs/plan.md Day 8).

App-owned, read-only, fixture-backed (ADR-0004, ADR-0009), like
`lookup_runbook`: `tool-gateway` imports this module by file path and
registers `TOOLS`; it must not import platform code. Allowlisted only for
`root-cause-summarizer`, which asks for the window before the alert fired.

The fixture stands in for a real change source (GitHub deploys, ArgoCD,
a CMDB): the input and output shapes are meant to survive that swap. Its
timestamps are absolute, as a real change log's are; an alert finds a
change only if it fired shortly after one.

A change matches when `service_or_host` is its `service` or one of its
`hosts`, and its timestamp is within [window_start, window_end], both ends
included. Most recent first.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

FIXTURE_PATH = Path(__file__).with_name("recent_changes.json")
MAX_WINDOW = timedelta(days=7)


class RecentChangesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_or_host: str = Field(
        min_length=1,
        max_length=253,
        description="A service name (e.g. 'checkout-svc') or a host (e.g. 'app-03').",
    )
    window_start: AwareDatetime = Field(
        description="Start of the window, ISO 8601 with a UTC offset, e.g. '2026-10-05T08:00:00Z'."
    )
    window_end: AwareDatetime = Field(
        description="End of the window (usually when the alert fired), ISO 8601 with a UTC offset."
    )

    @model_validator(mode="after")
    def _window(self) -> "RecentChangesInput":
        if self.window_end < self.window_start:
            raise ValueError("window_end is before window_start")
        if self.window_end - self.window_start > MAX_WINDOW:
            raise ValueError("window is longer than 7 days")
        return self


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(description="Stable reference to cite, e.g. 'argocd:checkout-svc@v2.3.1' or 'cmdb:CHG-20931'.")
    timestamp: AwareDatetime
    type: Literal["deploy", "config", "infra"]
    service: str
    hosts: list[str]
    author: str
    summary: str


class RecentChangesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_or_host: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    found: bool
    changes: list[Change]
    message: str | None = None


def _load_changes(path: Path) -> list[Change]:
    changes = [Change.model_validate(c) for c in json.loads(path.read_text())]
    refs = [c.ref for c in changes]
    if len(set(refs)) != len(refs):
        raise ValueError(f"{path}: duplicate ref")
    return changes


CHANGES = _load_changes(FIXTURE_PATH)


def recent_changes_lookup(args: RecentChangesInput) -> RecentChangesOutput:
    target = args.service_or_host
    matches = sorted(
        (
            c for c in CHANGES
            if (c.service == target or target in c.hosts) and args.window_start <= c.timestamp <= args.window_end
        ),
        key=lambda c: c.timestamp,
        reverse=True,
    )
    return RecentChangesOutput(
        service_or_host=target,
        window_start=args.window_start,
        window_end=args.window_end,
        found=bool(matches),
        changes=matches,
        message=None if matches else f"No recorded changes to '{target}' in this window.",
    )


TOOLS = {
    "recent-changes-lookup": {
        "version": "1.0.0",
        "description": (
            "List recorded deploys, config changes and infra changes to a service or host "
            "between window_start and window_end (most recent first). Returns found=false "
            "and no changes when nothing was recorded in that window."
        ),
        "input_model": RecentChangesInput,
        "output_model": RecentChangesOutput,
        "handler": recent_changes_lookup,
        "read_only": True,
    },
}
